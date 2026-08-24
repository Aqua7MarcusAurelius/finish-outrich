"""
Точка входа FastAPI.

Этап 2: шина событий, архивный писатель, SSE /events/stream.
Этап 3: модуль авторизации, менеджер воркеров, /system/proxy-check.
Этап 4: модуль истории (consumer шины, запись dialogs/messages/media),
         чистильщик файлов MinIO, endpoints /accounts/*/dialogs, /dialogs/*, /messages/*.
Этап 5: модуль транскрибации (OpenRouter chat-completions + ffmpeg-конвертер).
Этап 6: модуль описания медиа (GPT-4o для картинок/кадров, Gemini для документов).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.accounts import router as accounts_router
from api.routes.events import router as events_router
from api.routes.media import router as media_router
from api.routes.system import router as system_router
from core import bus, db
from core import minio as minio_mod
from core import redis as redis_mod
from core.config import settings
from modules.auth.routes import router as auth_router
from modules.auth.service import AuthService
from modules.autochat.report_consumer import ReportConsumer
from modules.autochat.routes import (
    dialog_autochat_router,
    router as autochat_router,
)
from modules.autochat.service import AutoChatService
from modules.dialog_statuses.routes import router as dialog_statuses_router
from modules.history.cleaner import Cleaner
from modules.history.routes import router as history_router
from modules.history.service import HistoryService
from modules.media_description.service import DescriptionService
from modules.lead_evaluator.routes import router as lead_evaluator_router
from modules.lead_evaluator.service import LeadEvaluatorService
from modules.lead_evaluator.silence_watcher import SilenceWatcher
from modules.partner_crm.finalizer import PartnerCrmFinalizer
from modules.partner_crm.poller import PartnerCrmPoller
from modules.partner_crm.routes import router as partner_crm_router
from modules.partner_crm.service import PartnerCrmService
from modules.peer_profile.service import PeerProfileService
from modules.report_bot.service import ReportBotService
from modules.transcription.service import TranscriptionService
from modules.worker_auto_runner.routes import router as auto_runner_router
from modules.worker_auto_runner.service import WorkerAutoRunnerService
from modules.worker_manager.routes import router as workers_router
from modules.worker_manager.service import WorkerManager

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────
    await db.init_pool()
    await redis_mod.init_client()
    await minio_mod.init_client()

    archive_task = asyncio.create_task(bus.archive_writer_loop())
    app.state.archive_task = archive_task

    history_service = HistoryService()
    app.state.history_service = history_service
    history_task = asyncio.create_task(history_service.run())
    app.state.history_task = history_task

    cleaner = Cleaner()
    app.state.cleaner = cleaner
    cleaner_task = asyncio.create_task(cleaner.run())
    app.state.cleaner_task = cleaner_task

    transcription_service = TranscriptionService()
    app.state.transcription_service = transcription_service
    transcription_task = asyncio.create_task(transcription_service.run())
    app.state.transcription_task = transcription_task

    description_service = DescriptionService()
    app.state.description_service = description_service
    description_task = asyncio.create_task(description_service.run())
    app.state.description_task = description_task

    app.state.auth_service = AuthService()
    app.state.worker_manager = WorkerManager()
    # Redis хранит status=running между рестартами app-контейнера, но
    # in-memory slot пустой — чистим до того как начнём принимать
    # /workers/* запросы, иначе UI видит несуществующий воркер.
    await app.state.worker_manager.reconcile_on_boot()

    # AutoChat: обращается к wrapper через worker_manager.get_wrapper.
    # Создаётся после WorkerManager, останавливается раньше него
    # (чтобы sender-loop успел закончить отправку до разрыва TG-сессий).
    autochat_service = AutoChatService(
        get_wrapper=app.state.worker_manager.get_wrapper,
    )
    app.state.autochat_service = autochat_service
    autochat_task = asyncio.create_task(autochat_service.run())
    app.state.autochat_task = autochat_task

    # PeerProfile: фоновый consumer, дотягивает bio + день рождения через
    # GetFullUserRequest на каждый dialog.created. Запускается после
    # WorkerManager (нужен get_wrapper), гасится до него (см. shutdown).
    peer_profile_service = PeerProfileService(
        get_wrapper=app.state.worker_manager.get_wrapper,
    )
    app.state.peer_profile_service = peer_profile_service
    peer_profile_task = asyncio.create_task(peer_profile_service.run())
    app.state.peer_profile_task = peer_profile_task

    # LeadEvaluator: LLM-судья закрытых диалогов + извлечение полей.
    # Consumer слушает autochat.finished_by_llm. Не зависит от других
    # сервисов кроме openrouter и dialog_statuses (через прямой импорт).
    lead_evaluator_service = LeadEvaluatorService()
    app.state.lead_evaluator_service = lead_evaluator_service
    lead_evaluator_task = asyncio.create_task(lead_evaluator_service.run())
    app.state.lead_evaluator_task = lead_evaluator_task

    # ReportConsumer: слушает dialog.status_changed, при терминальном
    # пузыре собирает HTML-отчёт и кладёт в MinIO в одну из трёх папок
    # (success/failed/archive). Заменил старый трюк "сразу после
    # <finishdialog/>" — теперь дожидаемся вердикта оценщика.
    report_consumer = ReportConsumer()
    app.state.report_consumer = report_consumer
    report_consumer_task = asyncio.create_task(report_consumer.run())
    app.state.report_consumer_task = report_consumer_task

    # ReportDeliveryService отключён 2026-08-23 — его заменил
    # ReportBotService (создаётся ниже, после partner_crm_service).
    # Старый слал те же отчёты сервис-аккаунтом и пушил статус
    # автоматически; вместе с ботом это дало бы дубль сообщения в группе
    # и предрешённый статус мимо модератора. Код модуля оставлен для
    # отката — вместе с REPORT_GROUP_MAP в .env.

    # SilenceWatcher: фоновый процесс переходов "Общение→Обрыв" по 4ч
    # тишины и "Первый контакт/Обрыв → Завершен провал" по 48ч.
    # Не зависит от lead_evaluator кроме общего settings/dialog_statuses.
    silence_watcher = SilenceWatcher()
    app.state.silence_watcher = silence_watcher
    silence_watcher_task = asyncio.create_task(silence_watcher.run())
    app.state.silence_watcher_task = silence_watcher_task

    # PartnerCrm: HTTP-клиент партнёрской CRM + локальный пул лидов.
    # Сервис держит httpx-клиент (keep-alive), poller докачивает пул из
    # CRM раз в `partner_crm.poll_interval_sec` если есть активные
    # CRM-воркеры и пул просел ниже `partner_crm.pool_low_threshold`.
    partner_crm_service = PartnerCrmService()
    app.state.partner_crm_service = partner_crm_service
    partner_crm_poller = PartnerCrmPoller(partner_crm_service)
    app.state.partner_crm_poller = partner_crm_poller
    partner_crm_poller_task = asyncio.create_task(partner_crm_poller.run())
    app.state.partner_crm_poller_task = partner_crm_poller_task

    # Финалайзер: слушает dialog.status_changed, при терминальном пузыре
    # и from_crm=TRUE пушит финальный статус в CRM. Реактивный, не
    # имеет состояния — может быть остановлен в любой момент.
    partner_crm_finalizer = PartnerCrmFinalizer(partner_crm_service)
    app.state.partner_crm_finalizer = partner_crm_finalizer
    partner_crm_finalizer_task = asyncio.create_task(partner_crm_finalizer.run())
    app.state.partner_crm_finalizer_task = partner_crm_finalizer_task

    # ReportBot (2026-08-23): публикует отчёты по успешным диалогам в
    # группы семейств С КНОПКАМИ модерации и откладывает пуш статуса в
    # CRM на 4 часа после последнего нажатия. Работает в bot-режиме —
    # inline-кнопки юзер-аккаунтам в MTProto недоступны, поэтому это не
    # сервис-аккаунт, а отдельный бот. Создаётся после partner_crm_service,
    # потому что через него финализирует лида (assign-l2 / status).
    # Без REPORT_BOT_TOKEN тихо выключается: успехи копятся в пузыре
    # «Модерация» и ждут, ничего не теряется.
    report_bot_service = ReportBotService(partner_crm_service)
    app.state.report_bot_service = report_bot_service
    report_bot_task = asyncio.create_task(report_bot_service.run())
    app.state.report_bot_task = report_bot_task

    # WorkerAutoRunner: per-account автомат старта диалогов по окну.
    # Создаётся ПОСЛЕ AutoChat, потому что зовёт его create_session.
    # При старте:
    #   * reset_on_boot гасит is_running у сессий, чьё session_ends_at
    #     истекло пока app лежал (или NULL — legacy-ряды);
    #   * resume_running_loops поднимает loop-task'и на оставшихся
    #     моментах для сессий, у которых session_ends_at ещё в будущем.
    auto_runner_service = WorkerAutoRunnerService(
        get_wrapper=app.state.worker_manager.get_wrapper,
        autochat_service=autochat_service,
        partner_crm_service=partner_crm_service,
    )
    app.state.auto_runner_service = auto_runner_service
    await auto_runner_service.reset_on_boot()
    await auto_runner_service.resume_running_loops()
    auto_runner_task = asyncio.create_task(auto_runner_service.run())
    app.state.auto_runner_task = auto_runner_task

    try:
        yield
    finally:
        # ── Shutdown ───────────────────────────────────────────────
        # WorkerAutoRunner гасим первым — чтобы он перестал звать
        # autochat.create_session, пока тот ещё в shutdown'е.
        try:
            await auto_runner_service.shutdown()
        except Exception:
            log.exception("auto_runner_service shutdown error")

        auto_runner_task.cancel()
        try:
            await auto_runner_task
        except asyncio.CancelledError:
            pass

        # AutoChat — следом: дадим активным сессиям дописать сегменты
        # до того как закроем Telegram-сессии.
        try:
            await autochat_service.stop()
        except Exception:
            log.exception("autochat_service stop error")

        autochat_task.cancel()
        try:
            await autochat_task
        except asyncio.CancelledError:
            pass

        # PeerProfile — до WorkerManager: пока wrapper'ы ещё живы, даём
        # consumer'у спокойно остановиться без падений на половине fetch'а.
        try:
            await peer_profile_service.stop()
        except Exception:
            log.exception("peer_profile_service stop error")

        peer_profile_task.cancel()
        try:
            await peer_profile_task
        except asyncio.CancelledError:
            pass

        try:
            await silence_watcher.stop()
        except Exception:
            log.exception("silence_watcher stop error")

        silence_watcher_task.cancel()
        try:
            await silence_watcher_task
        except asyncio.CancelledError:
            pass

        try:
            await report_consumer.stop()
        except Exception:
            log.exception("report_consumer stop error")

        report_consumer_task.cancel()
        try:
            await report_consumer_task
        except asyncio.CancelledError:
            pass

        try:
            await report_bot_service.stop()
        except Exception:
            log.exception("report_bot_service stop error")

        report_bot_task.cancel()
        try:
            await report_bot_task
        except asyncio.CancelledError:
            pass

        try:
            await lead_evaluator_service.stop()
        except Exception:
            log.exception("lead_evaluator_service stop error")

        lead_evaluator_task.cancel()
        try:
            await lead_evaluator_task
        except asyncio.CancelledError:
            pass

        try:
            await partner_crm_finalizer.stop()
        except Exception:
            log.exception("partner_crm_finalizer stop error")

        partner_crm_finalizer_task.cancel()
        try:
            await partner_crm_finalizer_task
        except asyncio.CancelledError:
            pass

        try:
            await partner_crm_poller.stop()
        except Exception:
            log.exception("partner_crm_poller stop error")

        partner_crm_poller_task.cancel()
        try:
            await partner_crm_poller_task
        except asyncio.CancelledError:
            pass

        try:
            await partner_crm_service.shutdown()
        except Exception:
            log.exception("partner_crm_service shutdown error")

        try:
            await app.state.worker_manager.shutdown()
        except Exception:
            log.exception("worker_manager shutdown error")

        try:
            await app.state.auth_service.shutdown()
        except Exception:
            log.exception("auth_service shutdown error")

        try:
            await description_service.stop()
        except Exception:
            log.exception("description_service stop error")

        description_task.cancel()
        try:
            await description_task
        except asyncio.CancelledError:
            pass

        try:
            await transcription_service.stop()
        except Exception:
            log.exception("transcription_service stop error")

        transcription_task.cancel()
        try:
            await transcription_task
        except asyncio.CancelledError:
            pass

        try:
            await cleaner.stop()
        except Exception:
            log.exception("cleaner stop error")

        cleaner_task.cancel()
        try:
            await cleaner_task
        except asyncio.CancelledError:
            pass

        try:
            await history_service.stop()
        except Exception:
            log.exception("history_service stop error")

        history_task.cancel()
        try:
            await history_task
        except asyncio.CancelledError:
            pass

        archive_task.cancel()
        try:
            await archive_task
        except asyncio.CancelledError:
            pass

        await db.close_pool()
        await redis_mod.close_client()


app = FastAPI(
    title="Telegram Automation Framework",
    version="2.0.0",
    docs_url="/docs" if settings.DOCS_PUBLIC else None,
    redoc_url="/redoc" if settings.DOCS_PUBLIC else None,
    openapi_url="/openapi.json" if settings.DOCS_PUBLIC else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Confirm-Delete", "Last-Event-ID"],
)

app.include_router(system_router)
app.include_router(events_router)
app.include_router(auth_router)
app.include_router(workers_router)
app.include_router(accounts_router)
app.include_router(history_router)
app.include_router(media_router)
app.include_router(autochat_router)
app.include_router(dialog_autochat_router)
app.include_router(dialog_statuses_router)
app.include_router(auto_runner_router)
app.include_router(partner_crm_router)
app.include_router(lead_evaluator_router)
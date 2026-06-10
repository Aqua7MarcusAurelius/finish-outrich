"""
Офлайн-самопроверка ReportDeliveryService — без Telegram, Docker и БД.

Подменяет границы (шина, БД, MinIO, Redis, враппер) фейками через
sys.modules и гоняет `_handle` по всем веткам:
  A. не-success отчёт → ничего не делаем
  B. success + есть группа → файл уходит, событие delivered, дедуп-флаг
  C. повтор того же диалога → дедуп, второй раз не шлём
  D. воркер без группы → событие skipped, дедуп не занимаем
  E. ошибка отправки → событие failed, claim снят (можно повторить)
  F. протухшая сессия → failed, подключение сброшено, claim снят

`core/events.py` берём настоящий — проверяем реальные имена событий.

Запуск:  python scripts/report_delivery_selftest.py
Код выхода 0 — все проверки прошли.
"""
import asyncio
import importlib.util
import logging
import os
import sys
import types
from datetime import datetime, timezone

# Глушим логи сервиса: ветки E/F намеренно зовут log.exception — это часть
# поведения, в выводе теста трейсбек был бы шумом и пугал «как будто упало».
logging.disable(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PUBLISHED: list[dict] = []   # все bus.publish
SENT: list[dict] = []        # все wrapper.send_file


# ── Фейки границ ────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self) -> None:
        self.store: dict = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


FAKE_REDIS = FakeRedis()
# account_id → имя воркера. 7 и 9 — разные воркеры одного семейства SAD;
# 8 — семейство без группы в карте.
WORKER_BY_ACCOUNT = {7: "SAD_03_RU", 8: "UNMAPPED_99", 9: "SAD_07_RU"}
CAPTION_ROW = {
    "username": "lead_user",
    "first_name": "Ирина",
    "last_name": "К",
    "telegram_user_id": 555,
    "extracted_name": "Ирина",
    "extracted_age": "34",
    "extracted_city": "Москва",
    "extracted_workplace": "турагентство",
    "extracted_interests": "путешествия, спорт",
    "evaluated_at": datetime(2026, 6, 9, 12, 30, tzinfo=timezone.utc),
}


class FakeConn:
    async def fetchval(self, query, *args):
        return WORKER_BY_ACCOUNT.get(args[0])

    async def fetchrow(self, query, *args):
        return CAPTION_ROW


class _AcquireCtx:
    async def __aenter__(self):
        return FakeConn()

    async def __aexit__(self, *a):
        return False


class FakePool:
    def acquire(self):
        return _AcquireCtx()


FAKE_POOL = FakePool()


async def fake_publish(**kwargs):
    PUBLISHED.append(kwargs)
    return {"id": "evt"}


async def fake_get_object(key, bucket=None):
    return b"<html>report</html>"


class FakeSettings:
    SERVICE_ACCOUNT_NAME = "service"
    MINIO_REPORTS_BUCKET = "autochat-reports"
    TELEGRAM_API_ID = 1
    TELEGRAM_API_HASH = "h"
    REPORT_GROUP_MAP = ""
    report_group_map: dict = {}


class SessionExpired(Exception):
    pass


class ProxyUnavailable(Exception):
    pass


class TelegramWrapper:  # в _handle не используется (только в _connect)
    pass


def install(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


core = install("core")
core.bus = install(
    "core.bus", publish=fake_publish, ensure_group=None, read_group=None,
    record_success=None, record_failure=None, ack_group=None,
)
core.db = install("core.db", get_pool=lambda: FAKE_POOL)
core.minio = install("core.minio", get_object=fake_get_object)
core.redis = install("core.redis", get_client=lambda: FAKE_REDIS)
core.config = install("core.config", settings=FakeSettings())

# Настоящий core/events.py — чтобы сверять реальные имена событий.
_spec = importlib.util.spec_from_file_location(
    "core.events", os.path.join(ROOT, "core", "events.py"),
)
events = importlib.util.module_from_spec(_spec)
sys.modules["core.events"] = events
_spec.loader.exec_module(events)
core.events = events

install("modules.worker")
install(
    "modules.worker.wrapper",
    TelegramWrapper=TelegramWrapper,
    SessionExpired=SessionExpired,
    ProxyUnavailable=ProxyUnavailable,
)

# Код под тестом — настоящий.
from modules.report_delivery.service import ReportDeliveryService  # noqa: E402


class FakeWrapper:
    def __init__(self, raise_exc=None):
        self.raise_exc = raise_exc

    def is_connected(self):
        return True

    async def disconnect(self):
        pass

    async def send_file(self, dialog, file, *, filename, caption=None,
                        force_document=True):
        if self.raise_exc is not None:
            raise self.raise_exc
        SENT.append({
            "dialog": dialog, "file": file, "filename": filename,
            "caption": caption, "force_document": force_document,
        })
        return {"telegram_message_id": 9001, "date": None}


def make_event(folder="success", account_id=7, dialog_id=101):
    worker = WORKER_BY_ACCOUNT.get(account_id, "SAD_03_RU")
    key = f"{folder}/{worker}_@lead_user_09-06-26_12-30.html"
    return {
        "id": "e1",
        "type": events.EventType.AUTOCHAT_REPORT_SAVED,
        "account_id": account_id,
        "data": {
            "folder": folder, "dialog_id": dialog_id, "minio_key": key,
            "session_id": 1, "messages_count": 10,
        },
    }


RESULTS: list[tuple[str, bool]] = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "  PASS " if cond else "  FAIL "
    print(mark + name + (f" — {detail}" if detail and not cond else ""))


def reset():
    PUBLISHED.clear()
    SENT.clear()
    FAKE_REDIS.store.clear()


def new_svc():
    svc = ReportDeliveryService()
    svc._group_map = {"SAD": -1001234567890}   # ключ — СЕМЕЙСТВО воркера
    svc._wrapper = FakeWrapper()
    return svc


async def main():
    # A. не success → ничего
    reset()
    svc = new_svc()
    await svc._handle(make_event(folder="failed"))
    check("A.не-success: send_file не вызван", len(SENT) == 0)
    check("A.не-success: событий не публиковали", len(PUBLISHED) == 0)

    # B. success + есть группа → доставка
    reset()
    svc = new_svc()
    await svc._handle(make_event(dialog_id=101))
    check("B.доставка: send_file ровно 1 раз", len(SENT) == 1)
    if SENT:
        s = SENT[0]
        check("B.чат верный", s["dialog"] == -1001234567890, str(s["dialog"]))
        check("B.имя файла из ключа",
              s["filename"] == "SAD_03_RU_@lead_user_09-06-26_12-30.html",
              s["filename"])
        check("B.отправлен документом", s["force_document"] is True)
        cap = s["caption"] or ""
        check("B.подпись: воркер", "SAD_03_RU" in cap)
        check("B.подпись: @партнёр", "@lead_user" in cap)
        check("B.подпись: поля оценщика",
              "Возраст: 34" in cap and "Москва" in cap, cap)
    check("B.событие delivered",
          any(p["type"] == events.EventType.REPORT_DELIVERED for p in PUBLISHED))
    check("B.дедуп-флаг выставлен", "report_delivered:101" in FAKE_REDIS.store)

    # C. повтор того же диалога → дедуп
    await svc._handle(make_event(dialog_id=101))
    check("C.дедуп: повторно не отправили", len(SENT) == 1)

    # B2. другой воркер того же семейства (SAD_07_RU) → та же группа SAD
    reset()
    svc = new_svc()
    await svc._handle(make_event(account_id=9, dialog_id=150))
    check("B2.семейство: send_file 1 раз", len(SENT) == 1)
    if SENT:
        check("B2.семейство: та же группа SAD", SENT[0]["dialog"] == -1001234567890)
        check("B2.семейство: имя файла этого воркера",
              SENT[0]["filename"] == "SAD_07_RU_@lead_user_09-06-26_12-30.html",
              SENT[0]["filename"])

    # D. семейство без группы → skipped
    reset()
    svc = new_svc()
    await svc._handle(make_event(account_id=8, dialog_id=202))
    check("D.skip: send_file не вызван", len(SENT) == 0)
    check("D.skip: событие skipped",
          any(p["type"] == events.EventType.REPORT_DELIVERY_SKIPPED for p in PUBLISHED))
    check("D.skip: дедуп не занят", "report_delivered:202" not in FAKE_REDIS.store)

    # E. ошибка отправки → failed + claim снят
    reset()
    svc = new_svc()
    svc._wrapper = FakeWrapper(raise_exc=RuntimeError("flood"))
    await svc._handle(make_event(dialog_id=303))
    check("E.fail: событие failed",
          any(p["type"] == events.EventType.REPORT_DELIVERY_FAILED for p in PUBLISHED))
    check("E.fail: claim снят (повтор возможен)",
          "report_delivered:303" not in FAKE_REDIS.store)

    # F. протухшая сессия → failed + сброс подключения
    reset()
    svc = new_svc()
    svc._wrapper = FakeWrapper(raise_exc=SessionExpired("expired"))
    await svc._handle(make_event(dialog_id=404))
    check("F.session: событие failed",
          any(p["type"] == events.EventType.REPORT_DELIVERY_FAILED for p in PUBLISHED))
    check("F.session: подключение сброшено", svc._wrapper is None)
    check("F.session: claim снят", "report_delivered:404" not in FAKE_REDIS.store)

    total = len(RESULTS)
    passed = sum(1 for _, c in RESULTS if c)
    print(f"\n=== {passed}/{total} проверок прошло ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

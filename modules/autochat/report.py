"""
HTML-отчёт по автодиалогу, завершённому LLM (`<finishdialog/>`).

Single-file HTML с встроенным CSS — открывается в браузере и читается
как переписка (пузыри: наши справа, собеседника слева). Заливается
в MinIO-бакет settings.MINIO_REPORTS_BUCKET, ссылка пишется в новую
таблицу autochat_reports.

Точка входа — save_report_for_session(session_id). Вызывается из
AutoChatService._on_session_finished_by_llm. Best-effort: ошибки
не пробрасываются наружу, публикуется autochat.report_failed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape
from typing import Any

from core import bus, db, minio
from core.config import settings
from core.events import EventType, Module, Status

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Загрузка данных из БД
# ─────────────────────────────────────────────────────────────────────

async def _load_session_context(session_id: int) -> dict[str, Any] | None:
    """
    Один запрос: сессия + воркер + диалог. None если сессии нет.
    Возвращает плоский dict со всем нужным для рендера.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                s.id                       AS session_id,
                s.account_id               AS account_id,
                s.dialog_id                AS dialog_id,
                s.telegram_user_id         AS telegram_user_id,
                s.target_username          AS target_username,
                s.created_at               AS started_at,
                s.updated_at               AS finished_at,
                s.status                   AS status,
                a.name                     AS worker_name,
                a.phone                    AS worker_phone,
                d.username                 AS partner_username,
                d.first_name               AS partner_first_name,
                d.last_name                AS partner_last_name
            FROM autochat_sessions s
            JOIN accounts a ON a.id = s.account_id
            LEFT JOIN dialogs d ON d.id = s.dialog_id
            WHERE s.id = $1
            """,
            session_id,
        )
    if row is None:
        return None
    return dict(row)


async def _load_messages(dialog_id: int) -> list[dict[str, Any]]:
    """
    Все сообщения диалога по дате. Берём только то, что нужно для рендера
    переписки — text, направление, время, тип. Медиа выводим как плейсхолдер.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                m.id,
                m.is_outgoing,
                m.type,
                m.date,
                m.text,
                EXISTS (SELECT 1 FROM media WHERE message_id = m.id) AS has_media
            FROM messages m
            WHERE m.dialog_id = $1
              AND m.deleted_at IS NULL
            ORDER BY m.date ASC, m.id ASC
            """,
            dialog_id,
        )
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# Форматирование значений
# ─────────────────────────────────────────────────────────────────────

def _format_duration(start: datetime, end: datetime) -> str:
    total = int((end - start).total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


def _format_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M")


def _format_date_sep(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y")


def _partner_display(ctx: dict[str, Any]) -> str:
    """Имя собеседника для шапки: '@uname (Иван Петров)' или fallback."""
    uname = ctx.get("partner_username") or ctx.get("target_username") or ""
    first = ctx.get("partner_first_name") or ""
    last = ctx.get("partner_last_name") or ""
    full = " ".join(p for p in (first, last) if p).strip()
    handle = f"@{uname}" if uname else f"id{ctx['telegram_user_id']}"
    if full:
        return f"{handle} ({full})"
    return handle


# ─────────────────────────────────────────────────────────────────────
# Рендер HTML
# ─────────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; }
body {
    margin: 0;
    background: #f0f2f5;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #111;
}
.wrap { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
.header {
    background: #fff;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.header h1 {
    margin: 0 0 4px 0;
    font-size: 18px;
    font-weight: 600;
}
.header .sub {
    color: #6b7280;
    font-size: 13px;
    margin-bottom: 12px;
}
.meta-grid {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 6px 16px;
    font-size: 13px;
}
.meta-grid dt { color: #6b7280; }
.meta-grid dd { margin: 0; color: #111; word-break: break-word; }
.chat {
    background: #fff;
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.date-sep {
    text-align: center;
    color: #6b7280;
    font-size: 12px;
    margin: 16px 0 12px;
    position: relative;
}
.date-sep span {
    background: #fff;
    padding: 0 10px;
    position: relative;
    z-index: 1;
}
.date-sep::before {
    content: "";
    position: absolute;
    left: 0; right: 0; top: 50%;
    height: 1px;
    background: #e5e7eb;
}
.row { display: flex; margin: 6px 0; }
.row.in  { justify-content: flex-start; }
.row.out { justify-content: flex-end; }
.bubble {
    max-width: 72%;
    padding: 8px 12px;
    border-radius: 14px;
    font-size: 14px;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
    position: relative;
}
.row.in  .bubble {
    background: #f1f3f5;
    border-bottom-left-radius: 4px;
}
.row.out .bubble {
    background: #d6f0ff;
    border-bottom-right-radius: 4px;
}
.bubble .time {
    display: block;
    font-size: 11px;
    color: #6b7280;
    margin-top: 4px;
    text-align: right;
}
.bubble.media-only { color: #6b7280; font-style: italic; }
.empty {
    text-align: center;
    color: #6b7280;
    padding: 32px 0;
    font-style: italic;
}
""".strip()


def _media_placeholder(msg_type: str) -> str:
    """Короткий ярлычок для сообщений без текста (фото/видео/etc)."""
    return {
        "photo": "[фото]",
        "video": "[видео]",
        "voice": "[голосовое]",
        "audio": "[аудио]",
        "sticker": "[стикер]",
        "animation": "[GIF]",
        "video_note": "[кружок]",
        "document": "[документ]",
    }.get(msg_type, "[медиа]")


def _render_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return '<div class="empty">В этом диалоге нет сообщений.</div>'

    out: list[str] = []
    last_date: str | None = None
    for m in messages:
        dt: datetime = m["date"]
        date_str = _format_date_sep(dt)
        if date_str != last_date:
            out.append(f'<div class="date-sep"><span>{escape(date_str)}</span></div>')
            last_date = date_str

        side = "out" if m["is_outgoing"] else "in"
        text = m.get("text") or ""
        has_media = m.get("has_media")
        if text:
            body = escape(text)
            media_cls = ""
        elif has_media:
            body = escape(_media_placeholder(m.get("type") or ""))
            media_cls = " media-only"
        else:
            # Сервисное / пустое сообщение — не показываем.
            continue

        time_str = _format_time(dt)
        out.append(
            f'<div class="row {side}">'
            f'<div class="bubble{media_cls}">'
            f'{body}<span class="time">{escape(time_str)}</span>'
            f'</div></div>'
        )
    if not out or all(s.startswith('<div class="date-sep"') for s in out):
        return '<div class="empty">В этом диалоге нет текстовых сообщений.</div>'
    return "\n".join(out)


def build_html(ctx: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """
    Собрать самодостаточный HTML. ctx — выход _load_session_context,
    messages — выход _load_messages.
    """
    started: datetime = ctx["started_at"]
    finished: datetime = ctx["finished_at"]
    our_count = sum(1 for m in messages if m["is_outgoing"])
    their_count = sum(1 for m in messages if not m["is_outgoing"])

    worker_label = ctx.get("worker_name") or f"account #{ctx['account_id']}"
    worker_phone = ctx.get("worker_phone") or ""
    partner = _partner_display(ctx)
    tg_id = ctx["telegram_user_id"]

    body = _render_messages(messages)

    return (
        f'<!doctype html>\n'
        f'<html lang="ru">\n'
        f'<head>\n'
        f'<meta charset="utf-8">\n'
        f'<title>Autochat session #{escape(str(ctx["session_id"]))} — {escape(partner)}</title>\n'
        f'<style>{_CSS}</style>\n'
        f'</head>\n'
        f'<body>\n'
        f'<div class="wrap">\n'
        f'  <div class="header">\n'
        f'    <h1>Автодиалог #{escape(str(ctx["session_id"]))}</h1>\n'
        f'    <div class="sub">Завершён LLM (&lt;finishdialog/&gt;)</div>\n'
        f'    <dl class="meta-grid">\n'
        f'      <dt>Воркер</dt><dd>{escape(worker_label)}'
        f'{(" — " + escape(worker_phone)) if worker_phone else ""}</dd>\n'
        f'      <dt>Собеседник</dt><dd>{escape(partner)} (tg_id={escape(str(tg_id))})</dd>\n'
        f'      <dt>Начало</dt><dd>{escape(_format_dt(started))}</dd>\n'
        f'      <dt>Финиш</dt><dd>{escape(_format_dt(finished))}</dd>\n'
        f'      <dt>Длительность</dt><dd>{escape(_format_duration(started, finished))}</dd>\n'
        f'      <dt>Сообщений</dt><dd>{our_count} наших / {their_count} от собеседника</dd>\n'
        f'    </dl>\n'
        f'  </div>\n'
        f'  <div class="chat">\n'
        f'{body}\n'
        f'  </div>\n'
        f'</div>\n'
        f'</body>\n'
        f'</html>\n'
    )


# ─────────────────────────────────────────────────────────────────────
# Внешняя точка входа
# ─────────────────────────────────────────────────────────────────────

def _build_storage_key(ctx: dict[str, Any]) -> str:
    """{YYYY}/{MM}/session_{id}.html — папки не пухнут."""
    dt: datetime = ctx["finished_at"]
    dt_utc = dt.astimezone(timezone.utc)
    return f"{dt_utc.year:04d}/{dt_utc.month:02d}/session_{ctx['session_id']}.html"


async def save_report_for_session(session_id: int) -> str | None:
    """
    Собрать HTML и положить в MinIO. Возвращает minio_key либо None
    при ошибке. Ошибки логируются + публикуется autochat.report_failed,
    но наружу не пробрасываются — это best-effort.
    """
    try:
        ctx = await _load_session_context(session_id)
        if ctx is None:
            log.warning("autochat report: session %s not found", session_id)
            return None
        dialog_id = ctx.get("dialog_id")
        messages: list[dict[str, Any]] = []
        if dialog_id is not None:
            messages = await _load_messages(int(dialog_id))

        html = build_html(ctx, messages)
        storage_key = _build_storage_key(ctx)
        data = html.encode("utf-8")

        await minio.put_object(
            storage_key,
            data,
            content_type="text/html; charset=utf-8",
            bucket=settings.MINIO_REPORTS_BUCKET,
        )

        pool = db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO autochat_reports (session_id, minio_key, messages_count)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_id) DO UPDATE
                    SET minio_key = EXCLUDED.minio_key,
                        messages_count = EXCLUDED.messages_count,
                        created_at = NOW()
                """,
                session_id, storage_key, len(messages),
            )

        await bus.publish(
            module=Module.AUTOCHAT,
            type=EventType.AUTOCHAT_REPORT_SAVED,
            status=Status.SUCCESS,
            account_id=int(ctx["account_id"]),
            data={
                "session_id": session_id,
                "minio_key": storage_key,
                "messages_count": len(messages),
            },
        )
        log.info(
            "autochat report saved: session=%s key=%s msgs=%d",
            session_id, storage_key, len(messages),
        )
        return storage_key

    except Exception as e:
        log.exception("autochat report: build/save failed for session %s", session_id)
        try:
            await bus.publish(
                module=Module.AUTOCHAT,
                type=EventType.AUTOCHAT_REPORT_FAILED,
                status=Status.ERROR,
                data={
                    "session_id": session_id,
                    "error": str(e)[:500],
                },
            )
        except Exception:
            log.exception(
                "autochat report: failed to publish report_failed for session %s",
                session_id,
            )
        return None

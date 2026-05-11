"""
HTML-отчёт по автодиалогу, завершённому LLM (`<finishdialog/>`).

Single-file HTML с встроенным CSS — открывается в браузере и читается
как переписка (пузыри в стиле Telegram, инициалы-аватарки, цитаты на
ответы). Заливается в MinIO-бакет settings.MINIO_REPORTS_BUCKET, ссылка
пишется в новую таблицу autochat_reports.

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
    Все сообщения диалога по дате. Для каждого тянем поля reply_* из
    цитируемого сообщения (LEFT JOIN messages на самого себя) — чтобы
    в HTML отрисовать цитату-полоску внутри пузыря.
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
                EXISTS (SELECT 1 FROM media WHERE message_id = m.id) AS has_media,
                p.id          AS reply_id,
                p.text        AS reply_text,
                p.is_outgoing AS reply_is_outgoing,
                p.type        AS reply_type,
                EXISTS (SELECT 1 FROM media WHERE message_id = p.id) AS reply_has_media
            FROM messages m
            LEFT JOIN messages p ON p.id = m.reply_to_message_id
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
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M")


_MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _format_date_sep(dt: datetime) -> str:
    """'11 мая 2026' — для плашки-разделителя дней."""
    d = dt.astimezone(timezone.utc)
    return f"{d.day} {_MONTHS_RU[d.month - 1]} {d.year}"


def _media_placeholder(msg_type: str) -> str:
    """Короткий ярлычок для сообщений без текста."""
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
# Аватарки: инициалы + стабильный цвет
# ─────────────────────────────────────────────────────────────────────

# 12 пастельных цветов — достаточно отличимых для двух собеседников,
# но не вырвиглазных в долгом отчёте.
_AVATAR_PALETTE = [
    "#e57373", "#f06292", "#ba68c8", "#9575cd",
    "#7986cb", "#64b5f6", "#4fc3f7", "#4db6ac",
    "#81c784", "#aed581", "#ffb74d", "#a1887f",
]


def _stable_color(seed: str) -> str:
    """Цвет аватарки — стабильный хэш по строке (per-process не плавает)."""
    if not seed:
        return _AVATAR_PALETTE[0]
    total = sum(ord(c) for c in seed)
    return _AVATAR_PALETTE[total % len(_AVATAR_PALETTE)]


def _initials(*parts: str | None) -> str:
    """Первые буквы из 1-2 непустых частей: 'Иван Петров' → 'ИП'."""
    chunks = [p.strip()[0] for p in parts if p and p.strip()]
    if not chunks:
        return "?"
    return "".join(chunks[:2]).upper()


def _partner_initials(ctx: dict[str, Any]) -> str:
    first = ctx.get("partner_first_name") or ""
    last = ctx.get("partner_last_name") or ""
    if first or last:
        return _initials(first, last)
    uname = ctx.get("partner_username") or ctx.get("target_username") or ""
    if uname:
        return _initials(uname)
    return "?"


def _partner_seed(ctx: dict[str, Any]) -> str:
    return (
        (ctx.get("partner_username") or "")
        + (ctx.get("partner_first_name") or "")
        + (ctx.get("partner_last_name") or "")
        + str(ctx.get("telegram_user_id") or "")
    )


def _worker_initials(ctx: dict[str, Any]) -> str:
    name = ctx.get("worker_name") or ""
    if name:
        # 'Worker One' → 'WO', 'worker' → 'W'
        parts = name.split()
        return _initials(*parts) if len(parts) > 1 else _initials(parts[0])
    phone = ctx.get("worker_phone") or ""
    return _initials(phone or "?")


def _worker_seed(ctx: dict[str, Any]) -> str:
    return (ctx.get("worker_name") or "") + str(ctx.get("account_id") or "")


# ─────────────────────────────────────────────────────────────────────
# CSS — встроенный, файл самодостаточный
# ─────────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    background:
        radial-gradient(circle at 20% 10%, rgba(99,179,237,0.18), transparent 40%),
        radial-gradient(circle at 80% 80%, rgba(186,104,200,0.16), transparent 40%),
        #eef2f7;
    background-attachment: fixed;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #14202d;
    -webkit-font-smoothing: antialiased;
    font-size: 14px;
    line-height: 1.45;
}
.wrap { max-width: 820px; margin: 0 auto; padding: 28px 16px 56px; }

/* ─── Header ─────────────────────────────────────────────────────── */
.header {
    background: #ffffff;
    border-radius: 16px;
    padding: 22px 24px 20px;
    box-shadow: 0 8px 24px rgba(20, 32, 45, 0.06);
    margin-bottom: 20px;
}
.header-top {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 18px;
}
.header-avatars {
    display: flex;
    align-items: center;
}
.header-avatars .avatar { width: 42px; height: 42px; font-size: 15px; }
.header-avatars .avatar + .avatar {
    margin-left: -10px;
    box-shadow: 0 0 0 3px #fff;
}
.header-title { flex: 1; min-width: 0; }
.header-title h1 {
    margin: 0;
    font-size: 17px;
    font-weight: 600;
    letter-spacing: -0.01em;
}
.header-title .sub {
    color: #6b7c93;
    font-size: 12.5px;
    margin-top: 4px;
}
.finish-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: #e7f5ee;
    color: #1f8b4c;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 500;
    white-space: nowrap;
}
.finish-badge::before {
    content: "";
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #1f8b4c;
}

/* ─── Метаданные-чипы ────────────────────────────────────────────── */
.chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 8px;
}
.chip {
    background: #f3f5f8;
    padding: 6px 11px;
    border-radius: 8px;
    font-size: 12px;
    color: #374151;
    line-height: 1.3;
}
.chip b { color: #14202d; font-weight: 600; }

/* ─── Chat container ─────────────────────────────────────────────── */
.chat {
    background: #ffffff;
    border-radius: 16px;
    padding: 18px 18px 24px;
    box-shadow: 0 8px 24px rgba(20, 32, 45, 0.06);
}

/* Разделитель дат — плашка по центру */
.date-sep {
    display: flex;
    justify-content: center;
    margin: 18px 0 14px;
}
.date-sep span {
    background: rgba(60, 80, 105, 0.12);
    color: #3c5069;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 500;
}

/* ─── Строка сообщения ───────────────────────────────────────────── */
.row {
    display: flex;
    align-items: flex-start;       /* аватарка — на уровне верха первого пузыря */
    gap: 10px;
    margin: 0;
}
.row.in  { justify-content: flex-start; }
.row.out { justify-content: flex-end; }

/* Дефолтный отступ между разными авторами — крупный */
.row + .row              { margin-top: 18px; }
/* Сжимаем подряд идущие сообщения одного автора */
.row + .row.same         { margin-top: 3px; }
.row.same .avatar        { visibility: hidden; }

.avatar {
    flex: none;
    width: 32px; height: 32px;
    border-radius: 50%;
    color: #fff;
    font-size: 12.5px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
    letter-spacing: -0.01em;
}

/* ─── Пузырь ─────────────────────────────────────────────────────── */
.bubble {
    max-width: 72%;
    padding: 9px 14px 7px;
    border-radius: 20px;           /* полностью округлая капсула */
    line-height: 1.5;
    word-wrap: break-word;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
}
.row.in  .bubble {
    background: #f1f4f8;
    color: #14202d;
}
.row.out .bubble {
    background: linear-gradient(135deg, #6cb4f5, #4a9eef);
    color: #fff;
}

.bubble .time {
    display: inline-block;
    font-size: 10.5px;
    opacity: 0.65;
    margin-left: 8px;
    float: right;
    margin-top: 4px;
    line-height: 1;
    white-space: nowrap;
}
.row.out .bubble .time { color: rgba(255,255,255,0.85); opacity: 1; }

.bubble.media-only {
    font-style: italic;
    opacity: 0.85;
}

/* ─── Цитата (reply) внутри пузыря ──────────────────────────────── */
.reply {
    border-left: 3px solid currentColor;
    padding: 3px 8px;
    margin-bottom: 5px;
    border-radius: 4px;
    background: rgba(255,255,255,0.18);
    font-size: 12px;
    line-height: 1.3;
    opacity: 0.92;
}
.row.in .reply {
    background: rgba(74, 158, 239, 0.08);
    color: #355d8a;
}
.row.out .reply {
    background: rgba(255,255,255,0.18);
    color: #f0f8ff;
}
.reply b { display: block; font-weight: 600; margin-bottom: 1px; font-size: 11.5px; }
.reply .qtext { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }

/* ─── Пустое состояние ───────────────────────────────────────────── */
.empty {
    text-align: center;
    color: #8a99af;
    padding: 36px 0;
    font-style: italic;
    font-size: 13px;
}

/* ─── Footer ─────────────────────────────────────────────────────── */
.footer {
    text-align: center;
    color: #8a99af;
    font-size: 11.5px;
    margin-top: 18px;
}
""".strip()


# ─────────────────────────────────────────────────────────────────────
# Рендер
# ─────────────────────────────────────────────────────────────────────

_REPLY_TEXT_MAX = 90


def _reply_snippet(text: str | None, has_media: bool, msg_type: str | None) -> str:
    """Текст в цитате-плашке: либо обрезанный text, либо ярлык медиа."""
    if text:
        snippet = text.strip().replace("\n", " ")
        if len(snippet) > _REPLY_TEXT_MAX:
            snippet = snippet[: _REPLY_TEXT_MAX - 1] + "…"
        return snippet
    if has_media:
        return _media_placeholder(msg_type or "")
    return "сообщение"


def _render_reply(m: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Маленькая плашка-цитата вверху пузыря, если у сообщения есть reply_to."""
    if m.get("reply_id") is None:
        return ""
    is_out = bool(m.get("reply_is_outgoing"))
    who = ctx.get("worker_name") or "Воркер" if is_out else (
        _partner_display(ctx).split(" (")[0]  # только handle, без full name
    )
    snippet = _reply_snippet(
        m.get("reply_text"),
        bool(m.get("reply_has_media")),
        m.get("reply_type"),
    )
    return (
        f'<div class="reply">'
        f'<b>{escape(who)}</b>'
        f'<span class="qtext">{escape(snippet)}</span>'
        f'</div>'
    )


def _render_messages(messages: list[dict[str, Any]], ctx: dict[str, Any]) -> str:
    if not messages:
        return '<div class="empty">В этом диалоге нет сообщений.</div>'

    p_initials = _partner_initials(ctx)
    p_color = _stable_color(_partner_seed(ctx))
    w_initials = _worker_initials(ctx)
    w_color = _stable_color(_worker_seed(ctx))

    out: list[str] = []
    last_date: str | None = None
    last_is_outgoing: bool | None = None
    rendered_any = False

    for m in messages:
        dt: datetime = m["date"]
        date_str = _format_date_sep(dt)
        if date_str != last_date:
            out.append(f'<div class="date-sep"><span>{escape(date_str)}</span></div>')
            last_date = date_str
            last_is_outgoing = None  # после разделителя — сбрасываем

        is_out = bool(m["is_outgoing"])
        text = m.get("text") or ""
        has_media = m.get("has_media")
        if text:
            body = escape(text)
            media_cls = ""
        elif has_media:
            body = escape(_media_placeholder(m.get("type") or ""))
            media_cls = " media-only"
        else:
            # Сервисное / пустое сообщение — пропускаем.
            continue

        side = "out" if is_out else "in"
        same = " same" if last_is_outgoing == is_out else ""

        if is_out:
            avatar_html = (
                f'<div class="avatar" style="background:{escape(w_color)};">'
                f'{escape(w_initials)}</div>'
            )
        else:
            avatar_html = (
                f'<div class="avatar" style="background:{escape(p_color)};">'
                f'{escape(p_initials)}</div>'
            )

        reply_html = _render_reply(m, ctx)
        time_html = f'<span class="time">{escape(_format_time(dt))}</span>'

        if is_out:
            # бабл слева, аватарка справа
            row_html = (
                f'<div class="row out{same}">'
                f'<div class="bubble{media_cls}">{reply_html}{body}{time_html}</div>'
                f'{avatar_html}'
                f'</div>'
            )
        else:
            row_html = (
                f'<div class="row in{same}">'
                f'{avatar_html}'
                f'<div class="bubble{media_cls}">{reply_html}{body}{time_html}</div>'
                f'</div>'
            )
        out.append(row_html)
        last_is_outgoing = is_out
        rendered_any = True

    if not rendered_any:
        return '<div class="empty">В этом диалоге нет текстовых сообщений.</div>'
    return "\n".join(out)


def build_html(ctx: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    """Самодостаточный HTML — открывается в браузере, читается как чат."""
    started: datetime = ctx["started_at"]
    finished: datetime = ctx["finished_at"]
    our_count = sum(1 for m in messages if m["is_outgoing"])
    their_count = sum(1 for m in messages if not m["is_outgoing"])

    worker_label = ctx.get("worker_name") or f"account #{ctx['account_id']}"
    worker_phone = ctx.get("worker_phone") or ""
    partner = _partner_display(ctx)
    tg_id = ctx["telegram_user_id"]
    duration = _format_duration(started, finished)

    p_color = _stable_color(_partner_seed(ctx))
    w_color = _stable_color(_worker_seed(ctx))
    p_init = _partner_initials(ctx)
    w_init = _worker_initials(ctx)

    body = _render_messages(messages, ctx)
    now_str = _format_dt(datetime.now(timezone.utc))

    return (
        '<!doctype html>\n'
        '<html lang="ru">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>Autochat #{escape(str(ctx["session_id"]))} — {escape(partner)}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n'
        '<body>\n'
        '  <div class="wrap">\n'
        '    <div class="header">\n'
        '      <div class="header-top">\n'
        '        <div class="header-avatars">\n'
        f'          <div class="avatar" style="background:{escape(w_color)};">{escape(w_init)}</div>\n'
        f'          <div class="avatar" style="background:{escape(p_color)};">{escape(p_init)}</div>\n'
        '        </div>\n'
        '        <div class="header-title">\n'
        f'          <h1>Автодиалог #{escape(str(ctx["session_id"]))}</h1>\n'
        f'          <div class="sub">{escape(worker_label)}'
        f'{(" · " + escape(worker_phone)) if worker_phone else ""} → {escape(partner)}</div>\n'
        '        </div>\n'
        '        <div class="finish-badge">завершён LLM</div>\n'
        '      </div>\n'
        '      <div class="chips">\n'
        f'        <span class="chip"><b>Старт</b> {escape(_format_dt(started))}</span>\n'
        f'        <span class="chip"><b>Финиш</b> {escape(_format_dt(finished))}</span>\n'
        f'        <span class="chip"><b>Длительность</b> {escape(duration)}</span>\n'
        f'        <span class="chip"><b>Сообщений</b> {our_count} наших · {their_count} от собеседника</span>\n'
        f'        <span class="chip"><b>tg_id</b> {escape(str(tg_id))}</span>\n'
        '      </div>\n'
        '    </div>\n'
        '    <div class="chat">\n'
        f'{body}\n'
        '    </div>\n'
        f'    <div class="footer">отчёт сгенерирован {escape(now_str)}</div>\n'
        '  </div>\n'
        '</body>\n'
        '</html>\n'
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

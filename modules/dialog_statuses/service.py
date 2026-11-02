"""
Сервис модуля dialog_statuses — каталог операторских пузырей и
присвоение их диалогам.

Две зоны ответственности:
  1. CRUD каталога (таблица `dialog_statuses`) — list/create/update/delete.
     Каждое изменение публикует событие `dialog_statuses.catalog_changed`.
  2. Присвоение статуса диалогу — `set_dialog_status(dialog_id, status_id)`.
     Пишет в `dialogs.status_id` и публикует `dialog.status_changed`.
     Эта функция — единственная точка входа: её зовут и UI (через ручку),
     и будущая автоматизация (listener событий типа autochat.finished_by_llm).

Параметр `source` хранится вместе с watermark последнего сообщения и
пробрасывается в JSONB-data события. Автоматические переходы могут
передать ожидаемый прежний статус для защиты от устаревшего решения.
"""
from __future__ import annotations

import re
from typing import Any

from core import bus, db
from core.events import EventType, Module, Status

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_LABEL_MIN, _LABEL_MAX = 1, 32
_UNSET = object()


class DialogStatusError(Exception):
    """Базовое исключение модуля. У наследников есть HTTP-эквиваленты."""
    code = "DIALOG_STATUS_ERROR"
    http_status = 400


class InvalidColor(DialogStatusError):
    code = "INVALID_COLOR"


class InvalidLabel(DialogStatusError):
    code = "INVALID_LABEL"


class StatusNotFound(DialogStatusError):
    code = "STATUS_NOT_FOUND"
    http_status = 404


class DialogNotFound(DialogStatusError):
    code = "DIALOG_NOT_FOUND"
    http_status = 404


def _validate_color(color: str) -> str:
    if not isinstance(color, str) or not _HEX_RE.match(color):
        raise InvalidColor("Цвет должен быть в формате #RRGGBB")
    return color.lower()


def _validate_label(label: str) -> str:
    if not isinstance(label, str):
        raise InvalidLabel("Лейбл должен быть строкой")
    cleaned = label.strip()
    if len(cleaned) < _LABEL_MIN or len(cleaned) > _LABEL_MAX:
        raise InvalidLabel(f"Лейбл должен быть длиной 1..{_LABEL_MAX} символов")
    return cleaned


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "label": row["label"],
        "color": row["color"],
        "sort_order": int(row["sort_order"]),
    }


# ─────────────────────────────────────────────────────────────────────
# Каталог
# ─────────────────────────────────────────────────────────────────────

async def list_statuses() -> list[dict[str, Any]]:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, label, color, sort_order FROM dialog_statuses "
            "ORDER BY sort_order ASC, id ASC"
        )
    return [_row_to_dict(r) for r in rows]


async def create_status(*, label: str, color: str, sort_order: int | None = None) -> dict[str, Any]:
    label = _validate_label(label)
    color = _validate_color(color)
    pool = db.get_pool()
    async with pool.acquire() as conn:
        if sort_order is None:
            # По умолчанию — в конец списка.
            row = await conn.fetchrow(
                """
                INSERT INTO dialog_statuses (label, color, sort_order)
                VALUES ($1, $2, COALESCE((SELECT MAX(sort_order) + 1 FROM dialog_statuses), 1))
                RETURNING id, label, color, sort_order
                """,
                label, color,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO dialog_statuses (label, color, sort_order)
                VALUES ($1, $2, $3)
                RETURNING id, label, color, sort_order
                """,
                label, color, int(sort_order),
            )
    result = _row_to_dict(row)
    await _publish_catalog_changed("created", result["id"])
    return result


async def update_status(
    status_id: int,
    *,
    label: str | None = None,
    color: str | None = None,
    sort_order: int | None = None,
) -> dict[str, Any]:
    if label is None and color is None and sort_order is None:
        # Нечего обновлять — просто возвращаем текущее состояние.
        existing = await get_status(status_id)
        return existing

    sets: list[str] = []
    params: list[Any] = [status_id]
    if label is not None:
        params.append(_validate_label(label))
        sets.append(f"label = ${len(params)}")
    if color is not None:
        params.append(_validate_color(color))
        sets.append(f"color = ${len(params)}")
    if sort_order is not None:
        params.append(int(sort_order))
        sets.append(f"sort_order = ${len(params)}")
    sets.append("updated_at = NOW()")

    sql = (
        f"UPDATE dialog_statuses SET {', '.join(sets)} "
        f"WHERE id = $1 RETURNING id, label, color, sort_order"
    )
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if row is None:
        raise StatusNotFound()
    result = _row_to_dict(row)
    await _publish_catalog_changed("updated", status_id)
    return result


async def delete_status(status_id: int) -> None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # ON DELETE SET NULL на dialogs.status_id сделает остальное.
        row = await conn.fetchrow(
            "DELETE FROM dialog_statuses WHERE id = $1 RETURNING id", status_id,
        )
    if row is None:
        raise StatusNotFound()
    await _publish_catalog_changed("deleted", status_id)


async def get_status(status_id: int) -> dict[str, Any]:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, label, color, sort_order FROM dialog_statuses WHERE id = $1",
            status_id,
        )
    if row is None:
        raise StatusNotFound()
    return _row_to_dict(row)


# ─────────────────────────────────────────────────────────────────────
# Присвоение статуса диалогу
# ─────────────────────────────────────────────────────────────────────

async def set_dialog_status(
    dialog_id: int,
    status_id: int | None,
    *,
    source: str = "manual",
    expected_status_id: int | None | object = _UNSET,
) -> dict[str, Any]:
    """
    Присвоить (или снять, если status_id=None) операторский статус диалогу.

    Единственная публичная точка входа. UI зовёт через ручку, будущая
    автоматизация (listener событий типа autochat.finished_by_llm) —
    напрямую функцией. Параметр `source` для аудита (что/кто проставил).

    Возвращает обновлённое состояние диалога: {id, status: {id, label, color} | null}.
    Бросает StatusNotFound / DialogNotFound.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if status_id is not None:
                exists = await conn.fetchval(
                    "SELECT 1 FROM dialog_statuses WHERE id = $1", status_id,
                )
                if not exists:
                    raise StatusNotFound()
            row = await conn.fetchrow(
                "SELECT id, account_id, status_id FROM dialogs WHERE id=$1 FOR UPDATE",
                dialog_id,
            )
            if row is None:
                raise DialogNotFound()
            changed = (expected_status_id is _UNSET
                       or row["status_id"] == expected_status_id)
            if changed:
                row = await conn.fetchrow(
                    """UPDATE dialogs SET status_id=$2, status_source=$3,
                           status_source_message_id=(
                               SELECT id FROM messages WHERE dialog_id=$1 AND deleted_at IS NULL
                               ORDER BY date DESC, telegram_message_id DESC, id DESC LIMIT 1),
                           updated_at=NOW()
                       WHERE id=$1 RETURNING id, account_id, status_id""",
                    dialog_id, status_id, source,
                )

            status_card: dict[str, Any] | None = None
            if row["status_id"] is not None:
                s = await conn.fetchrow(
                    "SELECT id, label, color, sort_order FROM dialog_statuses WHERE id = $1",
                    row["status_id"],
                )
                if s is not None:
                    status_card = _row_to_dict(s)

    if changed:
        await publish_status_changed(
            int(row["id"]), int(row["account_id"]), row["status_id"], source,
        )
    return {"id": int(row["id"]), "status": status_card, "changed": changed}


async def publish_status_changed(
    dialog_id: int, account_id: int, status_id: int | None, source: str,
) -> None:
    """Publish only after the caller's status/reason transaction commits."""

    await bus.publish(
        module=Module.DIALOG_STATUSES,
        type=EventType.DIALOG_STATUS_CHANGED,
        status=Status.SUCCESS,
        account_id=account_id,
        data={
            "dialog_id": dialog_id,
            "status_id": int(status_id) if status_id is not None else None,
            "source": source,
        },
    )

# ─────────────────────────────────────────────────────────────────────
# Внутреннее
# ─────────────────────────────────────────────────────────────────────

async def _publish_catalog_changed(action: str, status_id: int) -> None:
    await bus.publish(
        module=Module.DIALOG_STATUSES,
        type=EventType.DIALOG_STATUSES_CATALOG_CHANGED,
        status=Status.SUCCESS,
        data={"action": action, "id": int(status_id)},
    )

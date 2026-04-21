"""
Утилиты для Server-Sent Events.

Формат SSE — простой текст по HTTP:
    id: <id>
    event: <event_name>
    data: <строка или JSON>
    (пустая строка-разделитель)

Heartbeat — это комментарий ":": браузер его игнорирует, но соединение живёт.
"""
from __future__ import annotations

import json
from typing import Any


def sse_format(
    *,
    event: str | None = None,
    data: Any,
    id: str | None = None,
) -> bytes:
    """Сформировать одно SSE-сообщение как bytes."""
    lines: list[str] = []
    if id is not None:
        lines.append(f"id: {id}")
    if event is not None:
        lines.append(f"event: {event}")

    if isinstance(data, (dict, list)):
        data_str = json.dumps(data, ensure_ascii=False, default=str)
    else:
        data_str = str(data)

    # Если в data оказался перенос строки — каждая строка должна начинаться с "data: "
    for line in data_str.split("\n"):
        lines.append(f"data: {line}")

    # Двойной \n — разделитель событий в SSE
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def sse_heartbeat() -> bytes:
    """Комментарий-keepalive — строка должна начинаться с ':'."""
    return b": heartbeat\n\n"

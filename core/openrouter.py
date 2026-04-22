"""
Клиент OpenRouter.

У OpenRouter нет нативного Whisper STT-endpoint'а — используем
chat-completions с input_audio (поддерживается мультимодальными моделями
типа gpt-4o-audio-preview). Модель принимает base64-аудио и возвращает
текстовый ответ. Для задачи транскрипции просим её просто отдать расшифровку
без комментариев.

См. https://openrouter.ai/docs/guides/overview/multimodal/audio
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from core.config import settings

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Промпт: просим модель вернуть только текст сказанного, без префиксов.
TRANSCRIPTION_PROMPT = (
    "Transcribe the audio verbatim. Return only the spoken text, "
    "without any prefixes, comments or descriptions. If the audio is "
    "silent or unintelligible, return an empty string."
)

# HTTP-timeout для запросов. Whisper на длинных файлах может думать долго.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


class OpenRouterError(Exception):
    """Ошибка вызова OpenRouter (сетевая или бизнес)."""


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    audio_format: str = "mp3",
    model: str | None = None,
) -> str:
    """
    Отправить аудио-байты в OpenRouter и получить текстовую транскрипцию.

    `audio_format` — значение для поля format в input_audio. На текущий
    момент gpt-4o-audio-preview поддерживает только "wav" и "mp3", поэтому
    во всех случаях вызывающий должен предварительно конвертнуть в mp3.

    Возвращает строку. Пустая строка — валидный результат (тишина/невнятно).

    Бросает OpenRouterError при HTTP-ошибках и неожиданном формате ответа.
    """
    if not settings.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    model_name = model or settings.OPENROUTER_MODEL_TRANSCRIPTION
    b64 = base64.b64encode(audio_bytes).decode("ascii")

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIPTION_PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": b64,
                            "format": audio_format,
                        },
                    },
                ],
            }
        ],
        # Детерминированный ответ — нам не нужно творчество.
        "temperature": 0,
    }

    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Необязательные заголовки OpenRouter, но полезные для трекинга.
        "HTTP-Referer": "https://github.com/Aqua7MarcusAurelius/finish-outrich",
        "X-Title": "finish-outrich",
    }

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise OpenRouterError(f"network error: {e}") from e

    if resp.status_code >= 400:
        # Тело ошибки полезно в шине — укорачиваем до разумного.
        body = resp.text[:500]
        raise OpenRouterError(f"HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except Exception as e:
        raise OpenRouterError(f"invalid JSON: {e}") from e

    # Стандартный OpenAI-совместимый ответ:
    # { "choices": [ { "message": { "content": "..." } } ] }
    try:
        choices = data.get("choices") or []
        if not choices:
            # У OpenRouter ошибки иногда в поле "error" при 200 OK
            err = data.get("error")
            if err:
                raise OpenRouterError(f"openrouter error: {err}")
            raise OpenRouterError("empty choices in response")
        content = choices[0].get("message", {}).get("content")
    except OpenRouterError:
        raise
    except Exception as e:
        raise OpenRouterError(f"unexpected response shape: {e}") from e

    if content is None:
        return ""
    # content может прийти как строка или как список частей — нормализуем.
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    if not isinstance(content, str):
        content = str(content)

    return content.strip()
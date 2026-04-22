"""
Клиент OpenRouter.

Используем chat-completions (/api/v1/chat/completions) — у OpenRouter нет
отдельных endpoint'ов под Whisper STT или vision, всё идёт через единый
multimodal-интерфейс.

- Транскрипция  → gpt-4o-audio-preview (input_audio)
- Описание фото → gpt-4o (image_url с data-URL)
- Описание PDF  → Gemini (file с file_data), опционально плагин file-parser

См. https://openrouter.ai/docs/guides/overview/multimodal/pdfs
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from core.config import settings

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# ── Промпты ──────────────────────────────────────────────────────────

TRANSCRIPTION_PROMPT = (
    "Transcribe the audio verbatim. Return only the spoken text, "
    "without any prefixes, comments or descriptions. If the audio is "
    "silent or unintelligible, return an empty string."
)

# Для фото / стикеров / кадров из видео / GIF / video_note.
IMAGE_DESCRIPTION_PROMPT = (
    "Опиши кратко и по делу что изображено. Если кадров несколько — "
    "это один и тот же файл (видео/GIF/кружок), опиши общее содержание "
    "и что происходит, а не каждый кадр отдельно. Без префиксов и "
    "оговорок, только описание."
)

# Для документов (PDF и прочее, что поддерживает модель).
DOCUMENT_DESCRIPTION_PROMPT = (
    "Опиши что это за файл, что в нём содержится, передай общую суть. "
    "Без префиксов и оговорок, только описание."
)

# HTTP-timeout — длинные аудио/видео-файлы модель обрабатывает долго.
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)


class OpenRouterError(Exception):
    """Ошибка вызова OpenRouter (сетевая или бизнес)."""


# ── Приватные хелперы ────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Aqua7MarcusAurelius/finish-outrich",
        "X-Title": "finish-outrich",
    }


async def _post(payload: dict[str, Any]) -> str:
    """POST в OpenRouter → текст из message.content. Общий для всех методов."""
    if not settings.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=_headers())
    except httpx.HTTPError as e:
        raise OpenRouterError(f"network error: {e}") from e

    if resp.status_code >= 400:
        body = resp.text[:500]
        raise OpenRouterError(f"HTTP {resp.status_code}: {body}")

    try:
        data = resp.json()
    except Exception as e:
        raise OpenRouterError(f"invalid JSON: {e}") from e

    try:
        choices = data.get("choices") or []
        if not choices:
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


# ── Публичные функции ────────────────────────────────────────────────

async def transcribe_audio(
    audio_bytes: bytes,
    *,
    audio_format: str = "wav",
    model: str | None = None,
) -> str:
    """
    Отправить аудио-байты в OpenRouter и получить текстовую транскрипцию.

    `audio_format` — значение поля format у input_audio. gpt-4o-audio-preview
    формально принимает "wav" и "mp3", но провайдер OpenAI на mp3 иногда
    отвечает "not of valid mp3 format" — поэтому по умолчанию wav.

    Возвращает строку. Пустая строка — валидный результат (тишина/невнятно).
    """
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
                        "input_audio": {"data": b64, "format": audio_format},
                    },
                ],
            }
        ],
        "temperature": 0,
    }
    return await _post(payload)


async def describe_images(
    images: list[bytes],
    *,
    image_format: str = "jpeg",
    prompt: str | None = None,
    model: str | None = None,
) -> str:
    """
    Отправить одно или несколько изображений в vision-модель и получить
    текстовое описание. Используется для фото, стикеров, GIF/видео (кадры),
    video_note (кадры) — все кадры одного файла передаём в одном запросе.

    `image_format` — jpeg/png/webp, подставляется в data-URL.
    """
    if not images:
        raise OpenRouterError("describe_images called with empty list")

    model_name = model or settings.OPENROUTER_MODEL_DESCRIPTION
    text = prompt or IMAGE_DESCRIPTION_PROMPT

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/{image_format};base64,{b64}"},
        })

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
    }
    return await _post(payload)


async def describe_document(
    doc_bytes: bytes,
    *,
    filename: str,
    mime_type: str,
    prompt: str | None = None,
    model: str | None = None,
) -> str:
    """
    Отправить документ в модель с нативной поддержкой файлов (Gemini) и
    получить описание.

    По умолчанию модель `settings.OPENROUTER_MODEL_DESCRIPTION_DOCUMENTS`
    (google/gemini-2.5-flash — умеет PDF и часть других форматов нативно).

    Плагин `file-parser` с engine=native включён как страховка: если модель
    не умеет файл сама, OpenRouter попытается распарсить через провайдера
    файл-парсера. Если engine=native недоступен — OpenRouter подбирает
    доступный.
    """
    model_name = model or settings.OPENROUTER_MODEL_DESCRIPTION_DOCUMENTS
    text = prompt or DOCUMENT_DESCRIPTION_PROMPT
    b64 = base64.b64encode(doc_bytes).decode("ascii")

    payload: dict[str, Any] = {
        "model": model_name,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {
                        "type": "file",
                        "file": {
                            "filename": filename,
                            "file_data": f"data:{mime_type};base64,{b64}",
                        },
                    },
                ],
            }
        ],
        "plugins": [{"id": "file-parser", "pdf": {"engine": "native"}}],
        "temperature": 0,
    }
    return await _post(payload)

"""
Конвертация произвольного аудио/видео в mp3 через ffmpeg.

Зачем: gpt-4o-audio-preview (и совместимые OpenRouter-модели) принимают
input_audio только в wav или mp3. Telegram отдаёт голосовые как ogg/opus,
видео как mp4, видео-кружки тоже mp4 — единый путь «на вход любое, на
выход mp3» упрощает модуль.

Работаем строго через pipes — stdin → stdout. Никаких временных файлов.
FFmpeg присутствует в образе (см. Dockerfile).
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Аргументы ffmpeg:
#   -hide_banner -loglevel error  — тишина в stderr кроме реальных ошибок
#   -i pipe:0                     — читаем из stdin
#   -vn                           — игнорируем видеодорожку (для видео/кружков)
#   -ac 1                         — моно (для транскрипции больше не нужно)
#   -ar 16000                     — 16kHz достаточно для Whisper/gpt-4o-audio
#   -b:a 64k                      — умеренный битрейт
#   -f mp3                        — контейнер mp3
#   pipe:1                        — пишем в stdout
FFMPEG_ARGS = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-i", "pipe:0",
    "-vn",
    "-ac", "1",
    "-ar", "16000",
    "-b:a", "64k",
    "-f", "mp3",
    "pipe:1",
]

# На всякий случай — жёсткий таймаут на конвертацию.
# Час аудио перегнать — секунды, так что 120с с огромным запасом.
CONVERT_TIMEOUT = 120.0


class FfmpegError(Exception):
    """Ошибка при конвертации через ffmpeg."""


async def to_mp3(input_bytes: bytes) -> bytes:
    """
    Перегнать произвольное аудио/видео в mp3. Возвращает байты mp3.

    Бросает FfmpegError если процесс упал или не выдал ничего.
    """
    if not input_bytes:
        raise FfmpegError("empty input")

    try:
        proc = await asyncio.create_subprocess_exec(
            *FFMPEG_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise FfmpegError(f"ffmpeg not found: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_bytes),
            timeout=CONVERT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise FfmpegError(f"ffmpeg timeout after {CONVERT_TIMEOUT}s")

    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise FfmpegError(
            f"ffmpeg exit={proc.returncode}: {err or 'no stderr'}"
        )

    if not stdout:
        raise FfmpegError("ffmpeg produced no output")

    return stdout

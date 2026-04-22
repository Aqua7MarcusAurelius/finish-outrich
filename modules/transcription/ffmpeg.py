"""
Конвертация произвольного аудио/видео в wav (PCM 16kHz mono) через ffmpeg.

Зачем wav: gpt-4o-audio-preview/OpenAI капризит к mp3 (VBR/Xing-заголовки),
wav-PCM принимает всё что под Whisper.

Зачем временный файл: mp4 (Telegram video_note/video) хранит moov atom в
конце файла. ffmpeg при чтении из stdin не умеет seek, и аудиодорожку
в таких контейнерах просто не находит — на выходе остаётся только
пустой WAV-заголовок (44 байта). С файлом на диске ffmpeg seek'ает
нормально, и всё работает для любого формата входа.

Исключения:
  • FfmpegError   — реальная ошибка (битый файл, таймаут, ffmpeg exit!=0).
  • NoAudioError  — конвертация прошла, но аудиодорожки нет (тишина / немое
                    видео). Наверху трактуется как валидный пустой результат.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile

log = logging.getLogger(__name__)

# Аргументы ffmpeg (без -i — путь подставляем в рантайме):
#   -hide_banner -loglevel error  — тишина в stderr кроме реальных ошибок
#   -i <path>                     — вход из файла (с seek)
#   -vn                           — игнорируем видеодорожку
#   -ac 1                         — моно
#   -ar 16000                     — 16kHz (стандарт Whisper)
#   -c:a pcm_s16le                — 16-битный signed little-endian PCM
#   -f wav                        — контейнер wav
#   pipe:1                        — пишем в stdout
def _build_args(input_path: str) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        "-f", "wav",
        "pipe:1",
    ]


# Жёсткий таймаут на конвертацию.
CONVERT_TIMEOUT = 120.0

# WAV-header без данных ≈ 44 байта. Порог с запасом.
NO_AUDIO_THRESHOLD = 100


class FfmpegError(Exception):
    """Реальная ошибка при конвертации через ffmpeg."""


class NoAudioError(Exception):
    """В исходнике нет аудиодорожки — ffmpeg выдал только WAV-header."""


def _write_tmp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".bin", prefix="transcribe_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path
    except Exception:
        try:
            os.unlink(path)
        except Exception:
            pass
        raise


def _unlink_safe(path: str) -> None:
    try:
        os.unlink(path)
    except Exception:
        pass


async def to_wav(input_bytes: bytes) -> bytes:
    """
    Перегнать произвольное аудио/видео в wav (PCM 16kHz mono).
    Возвращает байты wav.

    Бросает:
      • FfmpegError  — при реальной ошибке ffmpeg.
      • NoAudioError — если в исходнике нет аудиодорожки.
    """
    if not input_bytes:
        raise FfmpegError("empty input")

    input_path = await asyncio.to_thread(_write_tmp, input_bytes)

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_build_args(input_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise FfmpegError(f"ffmpeg not found: {e}") from e

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
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

        # Пустой WAV (только header) — нет аудиодорожки. Не ошибка, а факт.
        if len(stdout) <= NO_AUDIO_THRESHOLD:
            raise NoAudioError(
                f"no audio track (wav size={len(stdout)} bytes)"
            )

        return stdout

    finally:
        await asyncio.to_thread(_unlink_safe, input_path)
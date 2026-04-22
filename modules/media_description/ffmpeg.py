"""
Нарезка кадров из видео / GIF / video_note через ffmpeg.

Возвращает N JPEG-байтов, равномерно распределённых по длительности файла.
Для vision-модели JPEG экономнее по payload, чем PNG (в base64 payload
растёт x1.33 — каждые лишние килобайты стоят лишних токенов на входе).

Почему через временный файл: ровно та же причина, что в
modules/transcription/ffmpeg.py — mp4 video_note от Telegram хранит
moov atom в конце, ffmpeg через stdin seek'ать не умеет. С файлом на
диске seek нормальный.

Алгоритм:
  1) ffprobe → длительность (секунды, float)
  2) N моментов равномерно: (i + 0.5) * duration / N для i in 0..N-1
     (середина каждого из N равных отрезков — не попадаем ровно в 0 и
     в конец, где часто чёрные кадры)
  3) На каждый момент — отдельный запуск ffmpeg с `-ss` и `-frames:v 1`

Зачем отдельные запуски, а не один `-vf fps=...`: так проще контролировать
количество кадров и их моменты. N обычно 3-5, оверхед на запуск процесса
небольшой и идёт параллельно.

Исключения:
  • FfmpegError   — реальная ошибка (битый файл, таймаут, exit!=0).
  • NoFramesError — длительность 0 / видеодорожки нет / все кадры пустые.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

log = logging.getLogger(__name__)

# Жёсткий таймаут на весь процесс нарезки.
EXTRACT_TIMEOUT = 120.0

# Таймаут на один подпроцесс (ffprobe или одна нарезка).
SUBPROCESS_TIMEOUT = 30.0

# Минимальный размер валидного JPEG — пустой/битый кадр обычно меньше.
MIN_FRAME_SIZE = 500


class FfmpegError(Exception):
    """Реальная ошибка при работе ffmpeg/ffprobe."""


class NoFramesError(Exception):
    """Не удалось получить ни одного валидного кадра (нет видео-дорожки)."""


def _write_tmp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".bin", prefix="frames_")
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


async def _run(args: list[str]) -> tuple[int, bytes, bytes]:
    """Запустить подпроцесс, вернуть (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise FfmpegError(f"{args[0]} not found: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=SUBPROCESS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise FfmpegError(f"{args[0]} timeout after {SUBPROCESS_TIMEOUT}s")

    return proc.returncode or 0, stdout or b"", stderr or b""


async def _probe_duration(path: str) -> float:
    """ffprobe → длительность в секундах. 0 если не удалось определить."""
    args = [
        "ffprobe",
        "-hide_banner",
        "-loglevel", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        path,
    ]
    rc, stdout, stderr = await _run(args)
    if rc != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        raise FfmpegError(f"ffprobe exit={rc}: {err or 'no stderr'}")
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        dur = float(data.get("format", {}).get("duration", 0) or 0)
    except Exception as e:
        raise FfmpegError(f"ffprobe bad json: {e}") from e
    return max(dur, 0.0)


async def _grab_frame(path: str, at_seconds: float) -> bytes:
    """
    Выдернуть один JPEG-кадр в момент at_seconds.

    `-ss` до `-i` — быстрый seek через контейнер (точность ~кейфрейм, нам
    хватает). `-frames:v 1` — ровно один кадр. `-q:v 3` — приличное
    качество JPEG при умеренном размере.
    """
    # -pix_fmt yuvj420p — MJPEG требует full-range YUV; без этого на
    # GIF/видео с yuv420p ffmpeg падает с
    # "Non full-range YUV is non-standard" → ff_frame_thread_encoder_init failed.
    args = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", f"{at_seconds:.3f}",
        "-i", path,
        "-frames:v", "1",
        "-pix_fmt", "yuvj420p",
        "-f", "image2",
        "-c:v", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]
    rc, stdout, stderr = await _run(args)
    if rc != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        raise FfmpegError(
            f"ffmpeg exit={rc} at {at_seconds:.3f}s: {err or 'no stderr'}"
        )
    return stdout


async def extract_frames(input_bytes: bytes, count: int) -> list[bytes]:
    """
    Нарезать `count` равномерно распределённых кадров из видео/GIF/video_note.
    Возвращает список JPEG-байтов (по одному на кадр) в хронологическом порядке.

    Бросает:
      • FfmpegError   — при реальной ошибке ffmpeg/ffprobe.
      • NoFramesError — если получить кадры не удалось (нет видео-дорожки,
        длительность 0, все кадры слишком маленькие).
    """
    if not input_bytes:
        raise FfmpegError("empty input")
    if count < 1:
        raise FfmpegError(f"invalid count={count}")

    input_path = await asyncio.to_thread(_write_tmp, input_bytes)

    async def _do() -> list[bytes]:
        duration = await _probe_duration(input_path)
        if duration <= 0:
            # Картинка-без-длительности / пустой файл / нет медиа.
            raise NoFramesError(f"non-positive duration ({duration})")

        # Середины N равных отрезков — не 0 и не конец.
        moments = [
            (i + 0.5) * duration / count for i in range(count)
        ]

        frames: list[bytes] = []
        for m in moments:
            # На очень коротких файлах `at >= duration` даст пустой результат —
            # зажимаем с небольшим отступом от конца.
            at = min(m, max(0.0, duration - 0.05))
            frame = await _grab_frame(input_path, at)
            if len(frame) >= MIN_FRAME_SIZE:
                frames.append(frame)

        if not frames:
            raise NoFramesError("no valid frames extracted")
        return frames

    try:
        return await asyncio.wait_for(_do(), timeout=EXTRACT_TIMEOUT)
    except asyncio.TimeoutError:
        raise FfmpegError(f"frame extraction timeout after {EXTRACT_TIMEOUT}s")
    finally:
        await asyncio.to_thread(_unlink_safe, input_path)

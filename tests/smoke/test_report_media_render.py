"""Smoke-тест Fix #6: рендер media-сообщений в HTML-отчёте использует
корректный `media.type`, не `messages.type`.

До фикса: `_media_body` смотрел на `msg.type` = 'regular' (тип сообщения)
и мапил на media-типы ('voice', 'photo', ...) — никогда не срабатывало,
всё падало в fallback `[медиа]`. Транскрипция и описание из БД тянулись
корректно, но не отрисовывались.

Тесты — на чистые функции с fake-словарями, без БД.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_report_media_render
"""
from __future__ import annotations

import sys
from modules.autochat.report import _media_body, _pick_media_type, _reply_snippet


def _fake_msg(*, media_types: str, transcription: str = "", description: str = "",
              text: str = "", has_media: bool = True, mtype: str = "regular") -> dict:
    return {
        "type": mtype,
        "text": text,
        "has_media": has_media,
        "media_types": media_types,
        "media_transcription": transcription,
        "media_description": description,
    }


def test_pick_media_type() -> list[tuple[str, str, str, bool]]:
    """(name, input_csv, expected, ok)."""
    cases: list[tuple[str, str, str]] = [
        ("voice-only",   "voice",              "voice"),
        ("audio-only",   "audio",              "audio"),
        ("photo-only",   "photo",              "photo"),
        ("video-only",   "video",              "video"),
        ("kruzhok",      "video_note",         "video_note"),
        # Приоритет audio > bimodal > visual
        ("audio+visual", "voice,photo",        "voice"),
        ("bimodal+visual","video,photo",       "video"),
        # Несколько media в группе — сортируем алфавитно внутри бакета
        ("multi-audio",  "audio,voice",        "audio"),  # a < v
        # Пустое / None
        ("empty",        "",                   ""),
        # Неизвестный тип — не мапим
        ("unknown",      "banana",             ""),
    ]
    return [(n, i, e, _pick_media_type({"media_types": i}) == e) for n, i, e in cases]


def test_media_body_matrix() -> list[tuple[str, str, bool]]:
    """(name, expected_body_start, ok)."""
    cases: list[tuple[str, dict, str]] = [
        # Голосовое с транскрипцией → лейбл + текст
        ("voice+transcription",
         _fake_msg(media_types="voice", transcription="Привет как дела"),
         '<span class="media-tag">[голосовое]</span> Привет как дела'),
        # Голосовое без транскрипции (например, не расшифровалось) → только ярлык
        ("voice-only-label",
         _fake_msg(media_types="voice", transcription=""),
         "[голосовое]"),
        # Фото с описанием
        ("photo+description",
         _fake_msg(media_types="photo", description="Селфи у окна"),
         '<span class="media-tag">[фото]</span> Селфи у окна'),
        # Видео с обоими каналами
        ("video-bimodal",
         _fake_msg(media_types="video", transcription="Что скажешь?",
                   description="Кадр из ролика: девушка машет"),
         '<span class="media-tag">[видео]</span> Что скажешь? · Кадр из ролика'),
        # Медиа группа: voice + photo → выбирает voice (audio-приоритет)
        ("mixed-audio+photo",
         _fake_msg(media_types="voice,photo", transcription="ок"),
         '<span class="media-tag">[голосовое]</span> ок'),
    ]
    return [
        (name, expected, _media_body(msg).startswith(expected))
        for name, msg, expected in cases
    ]


def test_reply_snippet_media() -> bool:
    """Цитата на голосовое сообщение показывает `[голосовое]`, не `[медиа]`."""
    got = _reply_snippet(text=None, has_media=True, media_types_csv="voice")
    return got == "[голосовое]"


def test_reply_snippet_text_preserved() -> bool:
    got = _reply_snippet(text="привет ок", has_media=True, media_types_csv="voice")
    return got == "привет ок"


def main() -> int:
    failures = 0
    print("=== _pick_media_type ===")
    for name, inp, expected, ok in test_pick_media_type():
        mark = "PASS" if ok else "FAIL"
        print(f"  {name:20} {inp!r:20} → {expected!r:12} {mark}")
        if not ok:
            failures += 1

    print()
    print("=== _media_body ===")
    for name, expected, ok in test_media_body_matrix():
        mark = "PASS" if ok else "FAIL"
        print(f"  {name:22} → {mark}")
        if not ok:
            failures += 1

    print()
    if test_reply_snippet_media():
        print("  reply_snippet media label → PASS")
    else:
        print("  reply_snippet media label → FAIL")
        failures += 1
    if test_reply_snippet_text_preserved():
        print("  reply_snippet text preserved → PASS")
    else:
        print("  reply_snippet text preserved → FAIL")
        failures += 1

    print()
    if failures:
        print(f"FAILED: {failures} test(s)")
        return 1
    print("ALL REPORT-MEDIA TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

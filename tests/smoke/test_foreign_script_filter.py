"""Smoke-тест Fix #24: фильтр CJK-символов в ответах LLM.

LLM изредка вставляет одиночный китайский иероглиф посреди русского
текста — артефакт семплинга. Лид видит «привет, как 好 дела» и понимает
что переписывается с ботом.

Проверяем:
  * CJK/Hangul/Katakana вырезаются;
  * эмодзи и русская пунктуация НЕ трогаются;
  * пробелы после вырезания нормализуются;
  * `parse_segments` применяет фильтр к каждому сегменту;
  * `sanitize_initial_response` фильтрует в обеих ветках.

Запуск:
    docker compose exec -T app python -m tests.smoke.test_foreign_script_filter
"""
from __future__ import annotations

import sys

from modules.autochat.generation import (
    parse_segments,
    sanitize_initial_response,
    strip_foreign_scripts,
)


def test_strips_cjk_mid_sentence() -> bool:
    """Одиночный иероглиф посреди русского текста — основной кейс."""
    got = strip_foreign_scripts("привет, как 好 дела")
    ok = got == "привет, как дела"
    if not ok:
        print(f"  FAIL: got {got!r}, expected 'привет, как дела'")
    return ok


def test_strips_various_scripts() -> bool:
    """Hiragana, Katakana, Hangul, CJK-пунктуация."""
    cases = [
        ("текст あ ещё", "текст ещё"),          # Hiragana
        ("текст ア ещё", "текст ещё"),          # Katakana
        ("текст 한 ещё", "текст ещё"),          # Hangul
        ("текст 。 ещё", "текст ещё"),          # CJK пунктуация
        ("текст ＡＢ ещё", "текст ещё"),        # Fullwidth latin
    ]
    all_ok = True
    for src, want in cases:
        got = strip_foreign_scripts(src)
        if got != want:
            print(f"  FAIL: {src!r} → {got!r}, expected {want!r}")
            all_ok = False
    return all_ok


def test_preserves_emoji_and_punctuation() -> bool:
    """Эмодзи и русская типографика должны выжить нетронутыми."""
    cases = [
        "привет 😊 как дела",
        "ну да — а что?",              # длинное тире
        "«цитата» и ещё…",             # ёлочки + многоточие
        "смайл ❤ и ✨ звёздочка",       # символы из U+2600–U+27BF
        "тест 🔥🎉 подряд",
    ]
    all_ok = True
    for src in cases:
        got = strip_foreign_scripts(src)
        if got != src:
            print(f"  FAIL: {src!r} изменился на {got!r}")
            all_ok = False
    return all_ok


def test_normalizes_spaces_after_strip() -> bool:
    """После вырезания не должно остаться двойных пробелов / пробела
    перед запятой."""
    cases = [
        ("привет 好 как дела", "привет как дела"),
        ("ага 好, понятно", "ага, понятно"),
        ("что 好 ?", "что?"),
    ]
    all_ok = True
    for src, want in cases:
        got = strip_foreign_scripts(src)
        if got != want:
            print(f"  FAIL: {src!r} → {got!r}, expected {want!r}")
            all_ok = False
    return all_ok


def test_clean_text_untouched() -> bool:
    """Чистый текст возвращается как есть (без лишних .strip())."""
    src = "обычный русский текст без мусора"
    return strip_foreign_scripts(src) == src


def test_parse_segments_applies_filter() -> bool:
    """parse_segments фильтрует каждый <msg>-сегмент."""
    resp = "<msg>привет 好 как дела</msg><msg>что нового ア</msg>"
    got = parse_segments(resp)
    want = ["привет как дела", "что нового"]
    ok = got == want
    if not ok:
        print(f"  FAIL: got {got!r}, expected {want!r}")
    return ok


def test_parse_segments_no_tags_fallback() -> bool:
    """Ответ без <msg>-тегов — фильтр всё равно применяется."""
    got = parse_segments("просто текст 好 без тегов")
    want = ["просто текст без тегов"]
    ok = got == want
    if not ok:
        print(f"  FAIL: got {got!r}, expected {want!r}")
    return ok


def test_sanitize_initial_filters() -> bool:
    """sanitize_initial_response фильтрует и с тегами, и без."""
    with_tags = sanitize_initial_response("<msg>привет 好 там</msg>")
    without = sanitize_initial_response("привет 好 там")
    ok = with_tags == "привет там" and without == "привет там"
    if not ok:
        print(f"  FAIL: with_tags={with_tags!r}, without={without!r}")
    return ok


def test_empty_and_none_safe() -> bool:
    """Пустые значения не роняют фильтр."""
    return (
        strip_foreign_scripts("") == ""
        and strip_foreign_scripts(None) is None
    )


def _run_all() -> int:
    results = [
        ("CJK посреди предложения вырезается", test_strips_cjk_mid_sentence()),
        ("Hiragana/Katakana/Hangul/fullwidth", test_strips_various_scripts()),
        ("эмодзи и типографика сохраняются", test_preserves_emoji_and_punctuation()),
        ("пробелы нормализуются", test_normalizes_spaces_after_strip()),
        ("чистый текст не трогается", test_clean_text_untouched()),
        ("parse_segments фильтрует сегменты", test_parse_segments_applies_filter()),
        ("parse_segments fallback без тегов", test_parse_segments_no_tags_fallback()),
        ("sanitize_initial_response фильтрует", test_sanitize_initial_filters()),
        ("пустые значения безопасны", test_empty_and_none_safe()),
    ]
    print()
    for name, ok in results:
        print(f"  {name:45} → {'PASS' if ok else 'FAIL'}")
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print()
    if passed == total:
        print(f"ALL {total} FOREIGN-SCRIPT TESTS PASSED")
        return 0
    print(f"FAILED: {total - passed}/{total}")
    return 1


if __name__ == "__main__":
    sys.exit(_run_all())

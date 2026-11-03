"""Offline privacy checks; no package service imports, DB or Telegram calls.

Run: python -m unittest tests.smoke.test_report_privacy
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("report_privacy_under_test", ROOT / "modules/report_bot/privacy.py")
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


def report(body: str, *, head: str = "", style: str = "") -> bytes:
    return f'<!doctype html><html lang="ru"><head>{head}<style>{style}</style></head><body>{body}</body></html>'.encode()


class ReportPrivacyTests(unittest.TestCase):
    def test_known_aliases_case_boundaries_and_unrelated_handles(self):
        text = "@Current User_OLD current_name @somebody CurrentExtra user_old2"
        actual = privacy.redact_text(text, ["current", "@USER_old", "current_name"])
        self.assertEqual(actual, f"{privacy.HIDDEN} {privacy.HIDDEN} {privacy.HIDDEN} @somebody CurrentExtra user_old2")

    def test_caption_numeric_id_and_unicode_preserved(self):
        self.assertEqual(privacy.redact_text("Иван 🟢 id123456 tg_id: 123456 / 91234567", [], 123456),
                         f"Иван 🟢 {privacy.HIDDEN} tg_id: {privacy.HIDDEN} / 91234567")

    def test_self_links_and_entities(self):
        for link in ("https://t.me/Lead_123", "t.me/lead_123", "https://telegram.me/lead_123",
                     "tg://resolve?domain=lead_123", "tg://user?id=123456",
                     "https://t.me/%6cead_123", "https%3A%2F%2Ft.me%2Flead_123",
                     "tg%253A%252F%252Fresolve%253Fdomain%253Dlead_123",
                     "&#64;lead_123", "&amp;#64;lead_123", "%40lead_123"):
            with self.subTest(link=link):
                self.assertEqual(privacy.redact_text(link, ["lead_123"], 123456), privacy.HIDDEN)

    def test_unrelated_escaping_remains_unchanged(self):
        text = "100%20 &amp; https://example.org/a%20b @another"
        self.assertEqual(privacy.redact_text(text, ["lead_123"]), text)

    def test_report_title_header_quotes_transcription_and_evaluation(self):
        blob = report('<h1>@LEAD_123</h1><div class="reply"><b>old_alias</b><span>&#108;ead_123</span></div>'
                      '<div>[голосовое] lead_123</div><div>Интересы: @old_alias</div><span>tg_id 123456</span>',
                      head="<title>Session — @lead_123</title>")
        actual = privacy.anonymize_report(blob, ["lead_123", "old_alias"], 123456).decode()
        for secret in ("lead_123", "old_alias", "123456"):
            self.assertNotIn(secret, actual.lower())
        self.assertIn("<title>Session — " + privacy.HIDDEN + "</title>", actual)
        self.assertIn('class="reply"', actual)
        self.assertIn("[голосовое]", actual)
        self.assertIn(b"LEAD_123", blob)  # original bytes untouched

    def test_urls_and_text_attributes_are_redacted(self):
        blob = report('<a href="https://t.me/%6cead_123" title="@lead_123">Открыть</a>'
                      '<a href="tg&#58;//user?id=123456">Профиль</a>'
                      '<img src="https://host/lead_123.jpg" alt="lead_123" />'
                      '<a href="https://example.org/other">Другой</a>')
        actual = privacy.anonymize_report(blob, ["lead_123"], 123456).decode()
        self.assertEqual(actual.count('href="#"'), 2)
        self.assertIn('src=""', actual)
        self.assertIn('href="https://example.org/other"', actual)
        self.assertIn('title="' + privacy.HIDDEN + '"', actual)

    def test_color_style_handles_do_not_corrupt_markup_or_css(self):
        css = ".color { color: red; font-style: normal; }"
        blob = report('<span class="color" id="style" style="color:blue">@style @color</span>', style=css)
        actual = privacy.anonymize_report(blob, ["style", "color"]).decode()
        self.assertIn("<style>" + css + "</style>", actual)
        self.assertIn('class="color" id="style" style="color:blue"', actual)
        self.assertIn(privacy.HIDDEN + " " + privacy.HIDDEN + "</span>", actual)

    def test_actual_report_static_css_is_supported(self):
        tree = ast.parse((ROOT / "modules/autochat/report.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "_CSS" for target in node.targets))
        css = ast.literal_eval(assignment.value.func.value).strip()
        actual = privacy.anonymize_report(report("@color", style=css), ["color"]).decode()
        self.assertIn("<style>" + css + "</style>", actual)

    def test_plain_text_is_escaped_without_injecting_markup(self):
        blob = report("&lt;script&gt;@lead_123&lt;/script&gt; &amp; другие")
        actual = privacy.anonymize_report(blob, ["lead_123"]).decode()
        self.assertIn("&lt;script&gt;" + privacy.HIDDEN + "&lt;/script&gt; &amp; другие", actual)
        self.assertNotIn("<script>", actual)

    def test_comments_do_not_retain_identities(self):
        actual = privacy.anonymize_report(report("<!-- @lead_123 -->ok"), ["lead_123"]).decode()
        self.assertNotIn("lead_123", actual)
        self.assertNotIn("<!--", actual)

    def test_fail_closed_invalid_bytes_and_html(self):
        for blob in (b"", b"\xff\xfe", b"not a report @lead_123", b"<html><body>@lead_123",
                     report("\x00"), report("<div></span>"), report("<script>lead_123</script>"),
                     report('<img onerror="lead_123">'), report("ok", style="a{background:url(https://host/lead_123)}")):
            with self.subTest(blob=blob):
                with self.assertRaises(ValueError):
                    privacy.anonymize_report(blob, ["lead_123"])

    def test_invalid_identity_fails_closed(self):
        for aliases, user_id in ((["bad/handle"], None), ([], True), ([], -1)):
            with self.subTest(aliases=aliases, user_id=user_id):
                with self.assertRaises(ValueError):
                    privacy.anonymize_report(report("ok"), aliases, user_id)

    def test_oversized_encoded_token_fails_closed_instead_of_splitting_identity(self):
        with self.assertRaises(ValueError):
            privacy.redact_text("x" * 4090 + "%40lead_123", ["lead_123"])


if __name__ == "__main__":
    unittest.main()

"""Redact a delivery copy; the canonical report in storage stays unchanged.

Only the supplied lead identities are hidden. This is not an anonymizer for
names, photographs, other people's handles, or text embedded inside pictures.
Malformed/unsupported reports raise ValueError: callers must never send the
original bytes as a fallback.
"""
from __future__ import annotations

import re
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import unquote

HIDDEN = "[контакт скрыт]"
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_TOKEN = re.compile(r"[^\s<>\"']+")
_TG_LINK = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"[^\s<>\"']+|tg://[^\s<>\"']+)", re.IGNORECASE,
)
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_ACTIVE = frozenset("script iframe object embed frame frameset base".split())
_STRUCTURAL = frozenset({"class", "id", "style"})
_URL_ATTRS = frozenset({"href", "src", "action", "poster", "data", "srcset", "formaction"})
_ACTIVE_CSS = re.compile(
    r'''url\s*\(|@import|expression\s*\(|(?<![-\w])content\s*:(?!\s*(?:""|'')\s*[;}])''', re.I,
)


def _identity_pattern(usernames: list[str], telegram_user_id: int | None) -> re.Pattern | None:
    aliases = set()
    for value in usernames:
        alias = value.strip().lstrip("@")
        if not alias:
            continue
        if not re.fullmatch(r"[a-zA-Z0-9_]{1,64}", alias):
            raise ValueError("Invalid report identity alias")
        aliases.add(alias.casefold())
    parts = []
    if aliases:
        names = "|".join(re.escape(a) for a in sorted(aliases, key=lambda a: (-len(a), a)))
        parts.append(r"(?<![a-zA-Z0-9_])@?(?:" + names + r")(?![a-zA-Z0-9_])")
    if telegram_user_id is not None:
        if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int) or telegram_user_id <= 0:
            raise ValueError("Invalid Telegram user id")
        parts.append(r"(?<!\d)(?:id)?" + str(telegram_user_id) + r"(?!\d)")
    return re.compile("|".join(parts), re.I) if parts else None


def _decoded(value: str) -> str:
    # Bounded decoding covers HTML entities and ordinary/double-encoded URLs.
    for _ in range(3):
        decoded = unescape(unquote(value))
        if decoded == value:
            break
        value = decoded
    return value


def _redact(value: str, pattern: re.Pattern | None) -> str:
    if pattern is None:
        return value

    def plain(text: str) -> str:
        text = _TG_LINK.sub(lambda m: HIDDEN if pattern.search(m[0]) else m[0], text)
        return pattern.sub(HIDDEN, text)

    def encoded(match: re.Match) -> str:
        token = match[0]
        if "%" not in token and "&" not in token:
            return token
        if len(token) > 4096:
            raise ValueError("Encoded report token is too large")
        decoded = _decoded(token)
        redacted = plain(decoded)
        # Preserve unrelated escaping and percent signs byte-for-byte.
        return redacted if redacted != decoded else token

    return plain(_TOKEN.sub(encoded, value))


def redact_text(text: str, usernames: list[str], telegram_user_id: int | None = None) -> str:
    """Hide known handles/IDs, including self-links, in a caption or text field."""
    if not isinstance(text, str):
        raise ValueError("Report text must be a string")
    return _redact(text, _identity_pattern(usernames, telegram_user_id))


class _ReportParser(HTMLParser):
    def __init__(self, pattern: re.Pattern | None):
        super().__init__(convert_charrefs=True)
        self.pattern = pattern
        self.output: list[str] = []
        self.stack: list[str] = []
        self.seen: set[str] = set()

    def handle_decl(self, decl: str) -> None:
        if decl.casefold() != "doctype html":
            raise ValueError("Unsupported report declaration")
        self.output.append("<!doctype html>")

    def unknown_decl(self, data: str) -> None:
        raise ValueError("Unsupported report declaration")

    def handle_pi(self, data: str) -> None:
        raise ValueError("Unsupported report processing instruction")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _ACTIVE:
            raise ValueError("Active content in report")
        self.seen.add(tag)
        rendered = []
        for key, value in attrs:
            if key.startswith("on") or (tag == "meta" and key == "http-equiv"):
                raise ValueError("Active attribute in report")
            if value is not None:
                if key == "style" and _ACTIVE_CSS.search(value):
                    raise ValueError("Unsupported report CSS")
                if key in _URL_ATTRS:
                    if self.pattern and self.pattern.search(_decoded(value)):
                        value = "#" if key == "href" else ""
                elif key not in _STRUCTURAL:
                    value = _redact(value, self.pattern)
            rendered.append(key if value is None else f'{key}="{escape(value, quote=True)}"')
        self.output.append("<" + tag + (" " + " ".join(rendered) if rendered else "") + ">")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            raise ValueError("Malformed report HTML")
        self.stack.pop()
        self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1] == "style":
            if _ACTIVE_CSS.search(data):
                raise ValueError("Unsupported report CSS")
            # CSS property names and selectors are not lead identifiers.
            self.output.append(data)
        else:
            self.output.append(escape(_redact(data, self.pattern), quote=False))

    # Comments are omitted rather than retaining possible hidden identifiers.


def anonymize_report(blob: bytes, usernames: list[str], telegram_user_id: int | None = None) -> bytes:
    """Return a redacted UTF-8 HTML copy, or raise ValueError (fail closed).

    The project's static CSS and structural class/id/style attributes remain
    intact even for handles such as ``style`` or ``color``. External CSS,
    generated CSS content, scripts and malformed markup are unsupported.
    """
    if not isinstance(blob, bytes) or not blob or len(blob) > _MAX_REPORT_BYTES:
        raise ValueError("Invalid report bytes")
    try:
        html = blob.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Report is not UTF-8") from exc
    if "\x00" in html:
        raise ValueError("Invalid report characters")
    parser = _ReportParser(_identity_pattern(usernames, telegram_user_id))
    parser.feed(html)
    parser.close()
    if parser.stack or not {"html", "body"}.issubset(parser.seen):
        raise ValueError("Incomplete report HTML")
    return "".join(parser.output).encode("utf-8")

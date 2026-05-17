"""Парсер ответа LLM-оценщика.

LLM может писать всё что угодно вне тегов — мы парсим только
структурированную часть. Регулярки нечувствительны к регистру и
работают по DOTALL (значения могут быть многострочными).

Возвращаемые поля все опциональны кроме `verdict`. Если verdict
не найден — caller делает ретрай.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Каждый тег матчится "не жадно", без вложенности.
_TAG_RE_BY_NAME = {
    "verdict": re.compile(r"<verdict>\s*(success|archive)\s*</verdict>",
                          re.IGNORECASE | re.DOTALL),
    "name":       re.compile(r"<name>(.*?)</name>",       re.IGNORECASE | re.DOTALL),
    "age":        re.compile(r"<age>(.*?)</age>",         re.IGNORECASE | re.DOTALL),
    "city":       re.compile(r"<city>(.*?)</city>",       re.IGNORECASE | re.DOTALL),
    "workplace":  re.compile(r"<workplace>(.*?)</workplace>", re.IGNORECASE | re.DOTALL),
    "interests":  re.compile(r"<interests>(.*?)</interests>", re.IGNORECASE | re.DOTALL),
}

# Максимумы — защита от того, что LLM напихает километровую строку.
_MAX_STR = 500


@dataclass
class EvaluationResult:
    verdict: str | None              # 'success' | 'archive' | None если не нашли
    name: str | None = None
    age: int | None = None
    city: str | None = None
    workplace: str | None = None
    interests: str | None = None
    parse_succeeded: bool = False

    def to_db_payload(self) -> dict:
        """Готово для INSERT в lead_evaluations (без id/timestamps)."""
        return {
            "verdict": self.verdict or "archive",
            "extracted_name": self.name,
            "extracted_age": self.age,
            "extracted_city": self.city,
            "extracted_workplace": self.workplace,
            "extracted_interests": self.interests,
            "parse_succeeded": self.parse_succeeded,
        }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_STR]


def _parse_age(raw: str | None) -> int | None:
    cleaned = _clean(raw)
    if cleaned is None:
        return None
    # Иногда LLM напишет "27 лет" — выудим первое число.
    match = re.search(r"\d{1,3}", cleaned)
    if not match:
        return None
    try:
        age = int(match.group(0))
    except ValueError:
        return None
    if not (10 <= age <= 120):
        return None
    return age


def parse_evaluator_response(raw: str) -> EvaluationResult:
    """
    Распарсить ответ LLM. Если verdict не найден — `parse_succeeded=False`
    и `verdict=None` (caller сделает ретрай).
    """
    if not raw:
        return EvaluationResult(verdict=None)

    verdict_match = _TAG_RE_BY_NAME["verdict"].search(raw)
    verdict = verdict_match.group(1).lower() if verdict_match else None

    name = _TAG_RE_BY_NAME["name"].search(raw)
    age = _TAG_RE_BY_NAME["age"].search(raw)
    city = _TAG_RE_BY_NAME["city"].search(raw)
    workplace = _TAG_RE_BY_NAME["workplace"].search(raw)
    interests = _TAG_RE_BY_NAME["interests"].search(raw)

    return EvaluationResult(
        verdict=verdict,
        name=_clean(name.group(1) if name else None),
        age=_parse_age(age.group(1) if age else None),
        city=_clean(city.group(1) if city else None),
        workplace=_clean(workplace.group(1) if workplace else None),
        interests=_clean(interests.group(1) if interests else None),
        parse_succeeded=verdict is not None,
    )

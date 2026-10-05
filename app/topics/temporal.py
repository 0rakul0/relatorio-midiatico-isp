from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date

from app.year_utils import find_years


MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def normalized_text(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", (text or "").lower())
        if not unicodedata.combining(char)
    )


def normalized_terms(text: str) -> set[str]:
    return {
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]+", normalized_text(text))
        if len(token) >= 3 and not token.isdigit()
    }


def requested_month_window(topic: str) -> tuple[date, date] | None:
    normalized = normalized_text(topic)
    pattern = re.compile(
        r"\b(" + "|".join(MONTHS_PT) + r")\s+(?:de\s+)?(20\d{2})(?!\d)"
    )
    matches: list[tuple[int, int]] = []
    for match in pattern.finditer(normalized):
        month_name, year_text = match.groups()
        prefix = normalized[max(0, match.start() - 24): match.start()]
        if month_name == "janeiro" and re.search(r"\brio\s+de\s+$", prefix):
            continue
        matches.append((MONTHS_PT[month_name], int(year_text)))

    unique = set(matches)
    if len(unique) != 1:
        return None
    month, year = unique.pop()
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def requested_year_window(topic: str) -> tuple[date, date] | None:
    normalized = normalized_text(topic)
    years = {int(year) for year in find_years(normalized)}
    if len(years) != 1:
        return None
    year = years.pop()
    return date(year, 1, 1), date(year, 12, 31)

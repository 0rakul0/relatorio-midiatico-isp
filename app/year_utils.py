"""Extração de ano em um único dialeto de regex.

Antes existiam duas formulações concorrentes (``\\b20\\d{2}\\b`` e
``(?<!\\d)(20\\d{2})(?!\\d)``) espalhadas pelo código, com comportamentos
diferentes perto de outros dígitos. Aqui fica a definição canônica.
"""

from __future__ import annotations

import re

YEAR_TOKEN = r"(?<!\d)(20\d{2})(?!\d)"
_YEAR_RE = re.compile(YEAR_TOKEN)


def find_year(text: str | None) -> str | None:
    """Retorna o primeiro ano de 4 dígitos isolado, se houver."""
    match = _YEAR_RE.search(text or "")
    return match.group(1) if match else None


def find_years(text: str | None) -> list[str]:
    """Retorna todos os anos de 4 dígitos isolados, na ordem de aparição."""
    return _YEAR_RE.findall(text or "")


def strip_year(text: str | None) -> str:
    """Remove anos de 4 dígitos e normaliza espaços em branco."""
    return " ".join(_YEAR_RE.sub("", text or "").split())

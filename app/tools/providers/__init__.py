"""Adapters dos provedores externos de pesquisa.

Este pacote é o único autorizado a falar diretamente com provedores externos.
DuckDuckGo é encapsulado por ``app.tools.search`` e Apify por
``app.tools.social``; nenhum service pode tocar provedores diretamente.
"""
from app.tools.providers.duckduckgo import (
    DuckDuckGoUnavailable,
    duckduckgo_available,
    fetch_url_text,
    search_news,
    search_text,
    search_videos,
)

__all__ = [
    "DuckDuckGoUnavailable",
    "duckduckgo_available",
    "fetch_url_text",
    "search_news",
    "search_text",
    "search_videos",
]

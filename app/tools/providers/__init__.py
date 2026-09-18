"""Adapters dos provedores externos de pesquisa.

Este pacote é o único autorizado a falar com SDKs/bibliotecas de busca
(``ddgs`` e ``tavily``). Deve ser importado exclusivamente por
``app.tools.search``; nenhum service pode tocar provedores diretamente.
"""
from app.tools.providers.duckduckgo import (
    DuckDuckGoUnavailable,
    duckduckgo_available,
    fetch_url_text,
    search_news,
    search_text,
    search_videos,
)
from app.tools.providers.tavily import is_tavily_hard_failure, tavily_search

__all__ = [
    "DuckDuckGoUnavailable",
    "duckduckgo_available",
    "fetch_url_text",
    "is_tavily_hard_failure",
    "search_news",
    "search_text",
    "search_videos",
    "tavily_search",
]

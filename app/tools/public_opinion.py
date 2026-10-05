"""Ferramenta determinística para descoberta e hidratação de pesquisas de opinião."""

from __future__ import annotations

from app.config import get_settings
from app.tools.providers.duckduckgo import (
    DuckDuckGoUnavailable,
    fetch_url_text,
    search_text,
)


def discover_public_opinion_sources(
    *,
    queries: list[str],
    results_per_query: int,
) -> list[dict]:
    settings = get_settings()
    found: dict[str, dict] = {}
    for query in queries:
        try:
            rows = search_text(
                query,
                max_results=max(1, int(results_per_query)),
                region=settings.duckduckgo_region,
                safesearch=settings.duckduckgo_safesearch,
                retries=settings.duckduckgo_max_retries,
                retry_base_seconds=settings.duckduckgo_retry_base_seconds,
            )
        except DuckDuckGoUnavailable:
            continue
        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url or url in found:
                continue
            found[url] = {**dict(row), "query": query}
    return list(found.values())


def hydrate_public_opinion_source(
    url: str,
    *,
    max_chars: int,
    timeout_seconds: float,
) -> str | None:
    return fetch_url_text(
        url,
        max_chars=max(1000, int(max_chars)),
        timeout=max(1.0, float(timeout_seconds)),
    )

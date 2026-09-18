"""Bulk article hydration tool exposed only to ReportAgent.

This module performs deterministic full-page retrieval for URLs that were
already collected and deduplicated. It does not search for new URLs. External
HTTP access remains behind an agent tool, preserving the project architecture.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.schemas import AgentBulkArticleFetchArgs, ArticleFetchToolResponse
from app.tools.providers import fetch_url_text


ArticleHydrationSink = Callable[[str, str | None, str, str | None], None]


def _fetch_one(url: str) -> tuple[str, str | None, str, str | None]:
    settings = get_settings()
    try:
        content = fetch_url_text(
            url,
            max_chars=settings.article_fetch_max_chars,
            timeout=settings.article_fetch_timeout_seconds,
        )
    except Exception as exc:  # best effort; a single page must not abort the batch
        return url, None, "ERROR", str(exc)[:1000]
    if content:
        return url, content, "FETCHED", None
    return url, None, "EMPTY_OR_BLOCKED", None


def make_bulk_article_fetch_tool(
    *,
    sink: ArticleHydrationSink | None = None,
) -> StructuredTool:
    def hidratar_artigos(urls: list[str]) -> dict[str, Any]:
        settings = get_settings()
        unique_urls = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
        unique_urls = unique_urls[: settings.article_fetch_max_items]

        outcomes: dict[str, tuple[str | None, str, str | None]] = {}
        with ThreadPoolExecutor(max_workers=settings.article_fetch_workers) as executor:
            futures = {executor.submit(_fetch_one, url): url for url in unique_urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    _url, content, status, error = future.result()
                except Exception as exc:  # defensive: future itself failed
                    content, status, error = None, "ERROR", str(exc)[:1000]
                outcomes[url] = (content, status, error)

        results: list[dict[str, Any]] = []
        # Persist in deterministic input order; SQLAlchemy sessions are not used
        # by worker threads.
        for url in unique_urls:
            content, status, error = outcomes.get(url, (None, "ERROR", "resultado ausente"))
            if sink is not None:
                sink(url, content, status, error)
            results.append(
                {
                    "url": url,
                    "status": status,
                    "chars": len(content or ""),
                    "error": error,
                }
            )

        return ArticleFetchToolResponse.model_validate({"results": results}).model_dump(mode="json")

    return StructuredTool.from_function(
        func=hidratar_artigos,
        args_schema=AgentBulkArticleFetchArgs,
        name="hidratar_artigos",
        description=(
            "Baixa em paralelo o texto completo de URLs ja coletadas e deduplicadas. "
            "Nao pesquisa novas URLs e nao altera o plano de busca."
        ),
        return_direct=False,
    )

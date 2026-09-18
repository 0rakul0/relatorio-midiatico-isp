"""Ferramentas de pesquisa oferecidas opcionalmente ao ReportAgent.

A tool não contém prompt nem lógica de decisão. Ela apenas executa a ação.
Prioridade dos provedores:
- web: DuckDuckGo News/Text -> Tavily;
- vídeo: DuckDuckGo Videos -> Tavily restrito a YouTube.

Este módulo orquestra a cadeia de provedores e normaliza as respostas. Os
adapters de SDK vivem em ``app.tools.providers`` (único ponto que fala com
``ddgs``/``tavily``); nenhum outro módulo pode importá-los diretamente. A
persistência é responsabilidade do sink fornecido pelo chamador.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.tools.providers import (
    DuckDuckGoUnavailable,
    duckduckgo_available,
    fetch_url_text,
    search_news as duckduckgo_news,
    search_text as duckduckgo_text,
    search_videos as duckduckgo_videos,
    tavily_search,
)
from app.schemas import (
    AgentSearchHit,
    AgentSearchResponse,
    AgentVideoSearchArgs,
    AgentWebSearchArgs,
)


SearchSink = Callable[[list[dict[str, Any]], str, str], list[dict[str, Any]]]
SearchContextResolver = Callable[[str], dict[str, Any] | None]


def search_providers_available() -> bool:
    """Indica se ao menos um provedor de pesquisa está configurado/instalado."""
    return duckduckgo_available() or bool(get_settings().tavily_api_key)


def _normalize_limit(value: int) -> int:
    return max(1, min(int(value or 5), 10))


def _search_web(
    query: str,
    *,
    max_results: int = 5,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    window_start: str | None = None,
    window_end: str | None = None,
    purpose: str = "MEDIA_REPERCUSSION",
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia de provedores web (uso interno das tools).

    ``providers`` controla a cadeia (ex.: ``("duckduckgo",)`` para forçar a
    busca apenas no DuckDuckGo e ``("tavily",)`` para usá-lo como único).
    """
    settings = get_settings()
    limit = min(_normalize_limit(max_results), max(1, get_settings().agent_search_max_results))

    if "duckduckgo" in providers:
        try:
            rows = duckduckgo_news(
                query,
                max_results=limit,
                region=settings.duckduckgo_region,
                safesearch=settings.duckduckgo_safesearch,
                retries=settings.duckduckgo_max_retries,
                retry_base_seconds=settings.duckduckgo_retry_base_seconds,
            )
            if not rows:
                rows = duckduckgo_text(
                    query,
                    max_results=limit,
                    region=settings.duckduckgo_region,
                    safesearch=settings.duckduckgo_safesearch,
                    retries=settings.duckduckgo_max_retries,
                    retry_base_seconds=settings.duckduckgo_retry_base_seconds,
                )
            if rows:
                normalized: list[dict[str, Any]] = []
                for row in rows[:limit]:
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    content = None
                    if settings.duckduckgo_fetch_pages:
                        content = fetch_url_text(
                            url,
                            max_chars=settings.duckduckgo_fetch_max_chars,
                            timeout=settings.duckduckgo_fetch_timeout_seconds,
                        )
                    normalized.append(
                        {
                            "title": str(row.get("title") or "Sem titulo"),
                            "url": url,
                            "snippet": row.get("snippet"),
                            "content": content or row.get("snippet"),
                            "published_at": row.get("published_at"),
                            "source_name": row.get("source_name"),
                            "provider": str(row.get("provider") or "duckduckgo"),
                        }
                    )
                if normalized:
                    return "duckduckgo", normalized
        except DuckDuckGoUnavailable:
            pass

    if "tavily" in providers:
        tavily = tavily_search(
            query,
            max_results=limit,
            window_start=window_start,
            window_end=window_end,
            purpose=purpose,
        )
        if tavily:
            return "tavily", tavily
    return "none", []


def _search_videos(
    query: str,
    *,
    max_results: int = 5,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    window_start: str | None = None,
    window_end: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia de provedores de vídeo (uso interno das tools).

    ``providers`` controla a cadeia (ex.: ``("duckduckgo",)`` para forçar a
    busca apenas no DuckDuckGo Videos e ``("tavily",)`` para usá-lo como único).
    """
    settings = get_settings()
    limit = min(_normalize_limit(max_results), max(1, get_settings().agent_search_max_results))

    if "duckduckgo" in providers:
        try:
            rows = duckduckgo_videos(
                query,
                max_results=limit,
                region=settings.duckduckgo_region,
                safesearch=settings.duckduckgo_safesearch,
                retries=settings.duckduckgo_max_retries,
                retry_base_seconds=settings.duckduckgo_retry_base_seconds,
            )
            if rows:
                normalized: list[dict[str, Any]] = []
                for row in rows[:limit]:
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    normalized.append(
                        {
                            "title": str(row.get("title") or "Video sem titulo"),
                            "url": url,
                            "snippet": row.get("description"),
                            "content": row.get("description"),
                            "published_at": row.get("published_at"),
                            "source_name": row.get("channel"),
                            "view_count": row.get("view_count"),
                            "provider": str(row.get("provider") or "duckduckgo_video"),
                        }
                    )
                if normalized:
                    return "duckduckgo", normalized
        except DuckDuckGoUnavailable:
            pass

    if "tavily" in providers:
        tavily = tavily_search(
            query,
            max_results=limit,
            video_only=True,
            window_start=window_start,
            window_end=window_end,
        )
        if tavily:
            return "tavily", tavily
    return "none", []


def _validated_response(
    *,
    query: str,
    provider: str,
    rows: list[dict[str, Any]],
    sink: SearchSink | None,
) -> dict[str, Any]:
    if sink and rows:
        rows = sink(rows, provider, query)

    hits = [AgentSearchHit.model_validate(row) for row in rows]
    return AgentSearchResponse(
        query=query,
        provider=provider,
        status="OK" if hits else "NO_RESULTS",
        results=hits,
    ).model_dump(mode="json")


def _execute_with_sink(
    *,
    query: str,
    providers: tuple[str, ...],
    run: Callable[[tuple[str, ...]], tuple[str, list[dict[str, Any]]]],
    sink: SearchSink | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia provedor a provedor consultando o sink a cada tentativa.

    O sink decide se o resultado é utilizável (guard, janela, canal, dedup).
    Se a tentativa atual não produzir nenhum item utilizável, tenta o próximo
    provedor (DuckDuckGo -> Tavily).
    """
    final_provider = "none"
    final_rows: list[dict[str, Any]] = []
    for provider_name in providers:
        provider, rows = run((provider_name,))
        if provider == "none":
            continue
        accepted = sink(rows, provider, query) if sink else rows
        final_provider, final_rows = provider, accepted
        if accepted:
            break
    return final_provider, final_rows


def make_web_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
) -> StructuredTool:
    def pesquisar_internet(query: str, max_results: int = 5) -> dict[str, Any]:
        options = (context(query) if context else None) or {}
        limit = int(options.get("max_results", max_results))
        window_start = options.get("window_start")
        window_end = options.get("window_end")
        purpose = str(options.get("purpose") or "MEDIA_REPERCUSSION")

        def run(chain: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
            return _search_web(
                query,
                max_results=limit,
                providers=chain,
                window_start=window_start,
                window_end=window_end,
                purpose=purpose,
            )

        provider, rows = _execute_with_sink(
            query=query, providers=providers, run=run, sink=sink
        )
        return _validated_response(query=query, provider=provider, rows=rows, sink=None)

    return StructuredTool.from_function(
        func=pesquisar_internet,
        args_schema=AgentWebSearchArgs,
        name="pesquisar_internet",
        description=(
            "Pesquisa informações atuais na internet e retorna fontes com título, URL, resumo e conteúdo quando disponível. "
            "A implementação tenta DuckDuckGo primeiro e Tavily somente quando necessário."
        ),
        return_direct=False,
    )


def make_video_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
) -> StructuredTool:
    def pesquisar_videos(query: str, max_results: int = 5) -> dict[str, Any]:
        options = (context(query) if context else None) or {}
        limit = int(options.get("max_results", max_results))
        window_start = options.get("window_start")
        window_end = options.get("window_end")

        def run(chain: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
            return _search_videos(
                query,
                max_results=limit,
                providers=chain,
                window_start=window_start,
                window_end=window_end,
            )

        provider, rows = _execute_with_sink(
            query=query, providers=providers, run=run, sink=sink
        )
        return _validated_response(query=query, provider=provider, rows=rows, sink=None)

    return StructuredTool.from_function(
        func=pesquisar_videos,
        args_schema=AgentVideoSearchArgs,
        name="pesquisar_videos",
        description=(
            "Pesquisa vídeos relevantes e retorna metadados e URLs. "
            "A implementação tenta DuckDuckGo Videos primeiro e Tavily somente quando necessário."
        ),
        return_direct=False,
    )

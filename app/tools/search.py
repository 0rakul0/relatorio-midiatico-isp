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
from threading import Lock
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
    AgentBulkSearchArgs,
    AgentBulkSearchResponse,
    AgentSearchHit,
    AgentSearchResponse,
    AgentVideoSearchArgs,
    AgentWebSearchArgs,
)


SearchSink = Callable[[list[dict[str, Any]], str, str], list[dict[str, Any]]]
SearchContextResolver = Callable[[str], dict[str, Any] | None]


class SearchObserver:
    """Recebe eventos granulares de cada tentativa para auditoria.

    A tool não conhece banco nem persistência: apenas notifica o observador a
    cada etapa (início da consulta, tentativa por provedor, resultado/erro e
    conclusão). Isso permite gravar ``SearchCall`` e atualizar a ``SearchQuery``
    sem violar a arquitetura ``TOOLS -> PROVIDERS``.
    """

    def query_started(self, *, query: str, tool_name: str) -> None:
        pass

    def provider_attempted(self, *, query: str, provider: str, tool_name: str) -> None:
        pass

    def provider_result(
        self,
        *,
        query: str,
        provider: str,
        tool_name: str,
        returned: int,
        accepted: int,
    ) -> None:
        pass

    def provider_error(
        self, *, query: str, provider: str, tool_name: str, error: str
    ) -> None:
        pass

    def query_finished(self, *, query: str, tool_name: str) -> None:
        pass


# Circuit breaker compartilhado do Tavily. Depois de uma falha dura
# (cota/rate limit/chave inválida), as consultas seguintes da execução usam
# apenas o DuckDuckGo. O estado é reiniciado no início de cada coleta.
_tavily_lock = Lock()
_tavily_disabled = False
_tavily_hard_failures = 0
_tavily_trips = 0


def reset_tavily_circuit_breaker() -> None:
    global _tavily_disabled, _tavily_hard_failures, _tavily_trips
    with _tavily_lock:
        _tavily_disabled = False
        _tavily_hard_failures = 0
        _tavily_trips = 0


def tavily_circuit_breaker_snapshot() -> dict[str, int | bool]:
    with _tavily_lock:
        return {
            "disabled": _tavily_disabled,
            "hard_failures": _tavily_hard_failures,
            "trips": _tavily_trips,
        }


def tavily_is_disabled() -> bool:
    with _tavily_lock:
        return _tavily_disabled


def _trip_tavily_circuit_breaker() -> None:
    global _tavily_disabled, _tavily_hard_failures, _tavily_trips
    with _tavily_lock:
        _tavily_hard_failures += 1
        if not _tavily_disabled:
            _tavily_disabled = True
            _tavily_trips += 1


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
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_internet",
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia de provedores web (uso interno das tools).

    ``providers`` controla a cadeia (ex.: ``("duckduckgo",)`` para forçar a
    busca apenas no DuckDuckGo e ``("tavily",)`` para usá-lo como único).
    O provedor devolvido é o último realmente tentado, mesmo com zero
    resultados, para que a tentativa seja auditável.
    """
    settings = get_settings()
    limit = min(_normalize_limit(max_results), max(1, get_settings().agent_search_max_results))
    attempted: str | None = None

    if "duckduckgo" in providers:
        if observer is not None:
            observer.provider_attempted(query=query, provider="duckduckgo", tool_name=tool_name)
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
            attempted = "duckduckgo"
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
        except DuckDuckGoUnavailable as exc:
            if observer is not None:
                observer.provider_error(
                    query=query, provider="duckduckgo", tool_name=tool_name, error=str(exc)
                )

    if "tavily" in providers and not tavily_is_disabled():
        if observer is not None:
            observer.provider_attempted(query=query, provider="tavily", tool_name=tool_name)
        try:
            tavily = tavily_search(
                query,
                max_results=limit,
                window_start=window_start,
                window_end=window_end,
                purpose=purpose,
            )
        except RuntimeError as exc:
            _trip_tavily_circuit_breaker()
            if observer is not None:
                observer.provider_error(
                    query=query, provider="tavily", tool_name=tool_name, error=str(exc)
                )
            tavily = []
        else:
            attempted = "tavily"
        if tavily:
            return "tavily", tavily
    return (attempted or "none"), []


def _search_videos(
    query: str,
    *,
    max_results: int = 5,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    window_start: str | None = None,
    window_end: str | None = None,
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_videos",
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia de provedores de vídeo (uso interno das tools).

    ``providers`` controla a cadeia (ex.: ``("duckduckgo",)`` para forçar a
    busca apenas no DuckDuckGo Videos e ``("tavily",)`` para usá-lo como único).
    """
    settings = get_settings()
    limit = min(_normalize_limit(max_results), max(1, get_settings().agent_search_max_results))
    attempted: str | None = None

    if "duckduckgo" in providers:
        if observer is not None:
            observer.provider_attempted(query=query, provider="duckduckgo", tool_name=tool_name)
        try:
            rows = duckduckgo_videos(
                query,
                max_results=limit,
                region=settings.duckduckgo_region,
                safesearch=settings.duckduckgo_safesearch,
                retries=settings.duckduckgo_max_retries,
                retry_base_seconds=settings.duckduckgo_retry_base_seconds,
            )
            attempted = "duckduckgo"
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
        except DuckDuckGoUnavailable as exc:
            if observer is not None:
                observer.provider_error(
                    query=query, provider="duckduckgo", tool_name=tool_name, error=str(exc)
                )

    if "tavily" in providers and not tavily_is_disabled():
        if observer is not None:
            observer.provider_attempted(query=query, provider="tavily", tool_name=tool_name)
        try:
            tavily = tavily_search(
                query,
                max_results=limit,
                video_only=True,
                window_start=window_start,
                window_end=window_end,
            )
        except RuntimeError as exc:
            _trip_tavily_circuit_breaker()
            if observer is not None:
                observer.provider_error(
                    query=query, provider="tavily", tool_name=tool_name, error=str(exc)
                )
            tavily = []
        else:
            attempted = "tavily"
        if tavily:
            return "tavily", tavily
    return (attempted or "none"), []


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
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_internet",
    stats: dict[str, int] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Executa a cadeia provedor a provedor consultando o sink a cada tentativa.

    O sink decide se o resultado é utilizável (guard, janela, canal, dedup).
    Se a tentativa atual não produzir nenhum item utilizável, tenta o próximo
    provedor (DuckDuckGo -> Tavily).
    """
    final_provider = "none"
    final_rows: list[dict[str, Any]] = []
    if stats is not None:
        stats["returned"] = 0
        stats["accepted"] = 0
    if observer is not None:
        observer.query_started(query=query, tool_name=tool_name)
    for provider_name in providers:
        provider, rows = run((provider_name,))
        if provider == "none":
            continue
        accepted = sink(rows, provider, query) if sink else rows
        if stats is not None:
            stats["returned"] = len(rows)
            stats["accepted"] = len(accepted)
        if observer is not None:
            observer.provider_result(
                query=query,
                provider=provider,
                tool_name=tool_name,
                returned=len(rows),
                accepted=len(accepted),
            )
        final_provider, final_rows = provider, accepted
        if accepted:
            break
    if observer is not None:
        observer.query_finished(query=query, tool_name=tool_name)
    return final_provider, final_rows


def _query_options(
    context: SearchContextResolver | None, query: str, default_limit: int
) -> tuple[int, str | None, str | None, str]:
    options = (context(query) if context else None) or {}
    limit = int(options.get("max_results", default_limit))
    return limit, options.get("window_start"), options.get("window_end"), str(
        options.get("purpose") or "MEDIA_REPERCUSSION"
    )


def make_web_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    def pesquisar_internet(query: str, max_results: int = 5) -> dict[str, Any]:
        limit, window_start, window_end, purpose = _query_options(context, query, max_results)

        def run(chain: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
            return _search_web(
                query,
                max_results=limit,
                providers=chain,
                window_start=window_start,
                window_end=window_end,
                purpose=purpose,
                observer=observer,
                tool_name="pesquisar_internet",
            )

        provider, rows = _execute_with_sink(
            query=query, providers=providers, run=run, sink=sink, observer=observer,
            tool_name="pesquisar_internet",
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
    observer: SearchObserver | None = None,
) -> StructuredTool:
    def pesquisar_videos(query: str, max_results: int = 5) -> dict[str, Any]:
        limit, window_start, window_end, _purpose = _query_options(context, query, max_results)

        def run(chain: tuple[str, ...]) -> tuple[str, list[dict[str, Any]]]:
            return _search_videos(
                query,
                max_results=limit,
                providers=chain,
                window_start=window_start,
                window_end=window_end,
                observer=observer,
                tool_name="pesquisar_videos",
            )

        provider, rows = _execute_with_sink(
            query=query, providers=providers, run=run, sink=sink, observer=observer,
            tool_name="pesquisar_videos",
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


def make_bulk_web_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    """Executa todas as consultas web planejadas com UMA chamada do agente.

    Diferente da tool individual, o retorno é compacto (sem título/conteúdo):
    o conteúdo completo já foi persistido determinísticamente pelo sink. Isso
    reduz drasticamente rodadas de LLM, tokens e risco de o agente omitir uma
    consulta do plano.
    """

    def executar_buscas_web(queries: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for query in list(queries)[:50]:
            limit, window_start, window_end, purpose = _query_options(context, query, 5)

            def run(
                chain: tuple[str, ...],
                query: str = query,
                limit: int = limit,
                window_start: str | None = window_start,
                window_end: str | None = window_end,
                purpose: str = purpose,
            ) -> tuple[str, list[dict[str, Any]]]:
                return _search_web(
                    query,
                    max_results=limit,
                    providers=chain,
                    window_start=window_start,
                    window_end=window_end,
                    purpose=purpose,
                    observer=observer,
                    tool_name="executar_buscas_web",
                )

            stats: dict[str, int] = {}
            provider, rows = _execute_with_sink(
                query=query,
                providers=providers,
                run=run,
                sink=sink,
                observer=observer,
                tool_name="executar_buscas_web",
                stats=stats,
            )
            results.append(
                {
                    "query": query,
                    "provider": provider,
                    "status": "OK" if rows else "NO_RESULTS",
                    "returned": int(stats.get("returned", 0)),
                    "accepted": int(stats.get("accepted", 0)),
                    "hits": [],
                }
            )
        return AgentBulkSearchResponse.model_validate({"results": results}).model_dump(mode="json")

    return StructuredTool.from_function(
        func=executar_buscas_web,
        args_schema=AgentBulkSearchArgs,
        name="executar_buscas_web",
        description=(
            "Executa em lote TODAS as consultas web do plano aprovado, na ordem recebida, "
            "sem criar, renomear ou omitir consultas. Retorna, por consulta, provedor, status, "
            "quantidade retornada e aceita; o conteúdo completo é persistido internamente."
        ),
        return_direct=False,
    )


def make_bulk_video_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    """Versão em lote da busca de vídeos para a coleta obrigatória."""

    def executar_buscas_videos(queries: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for query in list(queries)[:50]:
            limit, window_start, window_end, _purpose = _query_options(context, query, 5)

            def run(
                chain: tuple[str, ...],
                query: str = query,
                limit: int = limit,
                window_start: str | None = window_start,
                window_end: str | None = window_end,
            ) -> tuple[str, list[dict[str, Any]]]:
                return _search_videos(
                    query,
                    max_results=limit,
                    providers=chain,
                    window_start=window_start,
                    window_end=window_end,
                    observer=observer,
                    tool_name="executar_buscas_videos",
                )

            stats: dict[str, int] = {}
            provider, rows = _execute_with_sink(
                query=query,
                providers=providers,
                run=run,
                sink=sink,
                observer=observer,
                tool_name="executar_buscas_videos",
                stats=stats,
            )
            results.append(
                {
                    "query": query,
                    "provider": provider,
                    "status": "OK" if rows else "NO_RESULTS",
                    "returned": int(stats.get("returned", 0)),
                    "accepted": int(stats.get("accepted", 0)),
                    "hits": [],
                }
            )
        return AgentBulkSearchResponse.model_validate({"results": results}).model_dump(mode="json")

    return StructuredTool.from_function(
        func=executar_buscas_videos,
        args_schema=AgentBulkSearchArgs,
        name="executar_buscas_videos",
        description=(
            "Executa em lote TODAS as consultas de vídeo do plano aprovado, na ordem recebida, "
            "sem criar, renomear ou omitir consultas. Retorna, por consulta, provedor, status, "
            "quantidade retornada e aceita; os metadados completos são persistidos internamente."
        ),
        return_direct=False,
    )

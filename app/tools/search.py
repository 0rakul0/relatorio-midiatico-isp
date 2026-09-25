"""Search tools exposed to ReportAgent.

DuckDuckGo is the only search provider:
- web: DuckDuckGo News/Text
- video: DuckDuckGo Videos

Provider SDK adapters live only in app.tools.providers. Services never call
providers or LangChain tool.invoke() directly.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from langchain_core.tools import StructuredTool

from app.config import get_settings
from app.schemas import (
    AgentBulkSearchArgs,
    AgentBulkSearchResponse,
    AgentSearchHit,
    AgentSearchResponse,
    AgentVideoSearchArgs,
    AgentWebSearchArgs,
)
from app.tools.providers import (
    DuckDuckGoUnavailable,
    duckduckgo_available,
    search_news as duckduckgo_news,
    search_text as duckduckgo_text,
    search_videos as duckduckgo_videos,
)


SearchSink = Callable[[list[dict[str, Any]], str, str], list[dict[str, Any]]]
SearchContextResolver = Callable[[str], dict[str, Any] | None]


class SearchObserver:
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
        self,
        *,
        query: str,
        provider: str,
        tool_name: str,
        error: str,
    ) -> None:
        pass

    def query_finished(self, *, query: str, tool_name: str) -> None:
        pass


def search_providers_available() -> bool:
    return duckduckgo_available()


def _normalize_limit(value: int) -> int:
    return max(1, min(int(value or 5), 10))


def _search_web(
    query: str,
    *,
    max_results: int = 5,
    providers: tuple[str, ...] = ("duckduckgo",),
    window_start: str | None = None,
    window_end: str | None = None,
    purpose: str = "MEDIA_REPERCUSSION",
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_internet",
    raise_unavailable: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    settings = get_settings()
    limit = min(
        _normalize_limit(max_results),
        max(1, int(settings.agent_search_max_results)),
    )

    if "duckduckgo" in providers:
        if observer is not None:
            observer.provider_attempted(
                query=query,
                provider="duckduckgo",
                tool_name=tool_name,
            )
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
                    # Collection is intentionally light: preserve provider
                    # metadata/snippet now; full-page hydration happens only
                    # after URL consolidation in stage 4 (news validation).
                    normalized.append(
                        {
                            "title": str(row.get("title") or "Sem titulo"),
                            "url": url,
                            "snippet": row.get("snippet"),
                            "content": row.get("snippet"),
                            "published_at": row.get("published_at"),
                            "source_name": row.get("source_name"),
                            "provider": str(row.get("provider") or "duckduckgo"),
                        }
                    )
                if normalized:
                    return "duckduckgo", normalized
            return "duckduckgo", []
        except DuckDuckGoUnavailable as exc:
            if observer is not None:
                observer.provider_error(
                    query=query,
                    provider="duckduckgo",
                    tool_name=tool_name,
                    error=str(exc),
                )
            if raise_unavailable:
                raise
            return "duckduckgo", []

    return "none", []


def _search_videos(
    query: str,
    *,
    max_results: int = 5,
    providers: tuple[str, ...] = ("duckduckgo",),
    window_start: str | None = None,
    window_end: str | None = None,
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_videos",
    raise_unavailable: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    settings = get_settings()
    limit = min(
        _normalize_limit(max_results),
        max(1, int(settings.agent_search_max_results)),
    )

    if "duckduckgo" in providers:
        if observer is not None:
            observer.provider_attempted(
                query=query,
                provider="duckduckgo",
                tool_name=tool_name,
            )
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
            return "duckduckgo", []
        except DuckDuckGoUnavailable as exc:
            if observer is not None:
                observer.provider_error(
                    query=query,
                    provider="duckduckgo",
                    tool_name=tool_name,
                    error=str(exc),
                )
            if raise_unavailable:
                raise
            return "duckduckgo", []

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
    observer: SearchObserver | None = None,
    tool_name: str = "pesquisar_internet",
    stats: dict[str, int] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
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
    context: SearchContextResolver | None,
    query: str,
    default_limit: int,
) -> tuple[int, str | None, str | None, str]:
    options = (context(query) if context else None) or {}
    limit = int(options.get("max_results", default_limit))
    return (
        limit,
        options.get("window_start"),
        options.get("window_end"),
        str(options.get("purpose") or "MEDIA_REPERCUSSION"),
    )


def _skip_options(
    context: SearchContextResolver | None,
    query: str,
) -> tuple[bool, str | None]:
    options = (context(query) if context else None) or {}
    return bool(options.get("skip")), (
        str(options.get("skip_reason")) if options.get("skip_reason") else None
    )


def make_web_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo",),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    def pesquisar_internet(query: str, max_results: int = 5) -> dict[str, Any]:
        limit, window_start, window_end, purpose = _query_options(
            context,
            query,
            max_results,
        )

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
            query=query,
            providers=providers,
            run=run,
            sink=sink,
            observer=observer,
            tool_name="pesquisar_internet",
        )
        return _validated_response(
            query=query,
            provider=provider,
            rows=rows,
            sink=None,
        )

    return StructuredTool.from_function(
        func=pesquisar_internet,
        args_schema=AgentWebSearchArgs,
        name="pesquisar_internet",
        description=(
            "Pesquisa informacoes atuais na internet usando DuckDuckGo "
            "(noticias e texto)."
        ),
        return_direct=False,
    )


def make_video_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo",),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    def pesquisar_videos(query: str, max_results: int = 5) -> dict[str, Any]:
        limit, window_start, window_end, _purpose = _query_options(
            context,
            query,
            max_results,
        )

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
            query=query,
            providers=providers,
            run=run,
            sink=sink,
            observer=observer,
            tool_name="pesquisar_videos",
        )
        return _validated_response(
            query=query,
            provider=provider,
            rows=rows,
            sink=None,
        )

    return StructuredTool.from_function(
        func=pesquisar_videos,
        args_schema=AgentVideoSearchArgs,
        name="pesquisar_videos",
        description="Pesquisa videos relevantes usando DuckDuckGo Videos.",
        return_direct=False,
    )


def make_bulk_web_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo",),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    """Execute the complete approved plan in one agent tool call.

    Approved queries are not skipped because a corpus target was reached.
    ``skip=True`` is reserved for deterministic plan-integrity guards, such as
    a hallucinated query that is not part of the approved plan.
    """

    def executar_buscas_web(queries: list[str]) -> dict[str, Any]:
        """Busca em paralelo; persiste e audita sequencialmente na thread principal."""
        settings = get_settings()
        requested = list(queries)
        result_by_index: dict[int, dict[str, Any]] = {}
        active: list[dict[str, Any]] = []

        for index, query in enumerate(requested):
            skip, skip_reason = _skip_options(context, query)
            if skip:
                if observer is not None:
                    observer.query_started(query=query, tool_name="executar_buscas_web")
                    observer.query_finished(query=query, tool_name="executar_buscas_web")
                result_by_index[index] = {
                    "query": query,
                    "provider": "none",
                    "status": "SKIPPED",
                    "error": skip_reason,
                    "returned": 0,
                    "accepted": 0,
                    "hits": [],
                }
                continue

            limit, window_start, window_end, purpose = _query_options(context, query, 5)
            active.append(
                {
                    "index": index,
                    "query": query,
                    "limit": limit,
                    "window_start": window_start,
                    "window_end": window_end,
                    "purpose": purpose,
                }
            )

        if active:
            workers = (
                min(len(active), max(1, int(settings.search_parallel_web_workers)))
                if settings.search_parallel_enabled
                else 1
            )
            futures: dict[int, Future] = {}

            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="media-web",
            ) as executor:
                for item in active:
                    query = str(item["query"])
                    if observer is not None:
                        # A Session SQLAlchemy do observer continua exclusivamente
                        # na thread principal. As workers fazem somente I/O externo.
                        observer.query_started(query=query, tool_name="executar_buscas_web")
                        if "duckduckgo" in providers:
                            observer.provider_attempted(
                                query=query,
                                provider="duckduckgo",
                                tool_name="executar_buscas_web",
                            )

                    futures[int(item["index"])] = executor.submit(
                        _search_web,
                        query,
                        max_results=int(item["limit"]),
                        providers=providers,
                        window_start=item["window_start"],
                        window_end=item["window_end"],
                        purpose=str(item["purpose"]),
                        observer=None,
                        tool_name="executar_buscas_web",
                        raise_unavailable=True,
                    )

                # Mantém a persistência/auditoria na ordem do plano, mesmo que
                # as respostas HTTP terminem em ordem diferente.
                for item in active:
                    index = int(item["index"])
                    query = str(item["query"])
                    try:
                        provider, raw_rows = futures[index].result()
                        accepted_rows = sink(raw_rows, provider, query) if sink else raw_rows
                        if observer is not None:
                            observer.provider_result(
                                query=query,
                                provider=provider,
                                tool_name="executar_buscas_web",
                                returned=len(raw_rows),
                                accepted=len(accepted_rows),
                            )
                            observer.query_finished(
                                query=query,
                                tool_name="executar_buscas_web",
                            )
                        result_by_index[index] = {
                            "query": query,
                            "provider": provider,
                            "status": "OK" if accepted_rows else "NO_RESULTS",
                            "returned": len(raw_rows),
                            "accepted": len(accepted_rows),
                            "hits": [],
                        }
                    except Exception as exc:
                        if observer is not None:
                            observer.provider_error(
                                query=query,
                                provider="duckduckgo",
                                tool_name="executar_buscas_web",
                                error=str(exc),
                            )
                            observer.query_finished(
                                query=query,
                                tool_name="executar_buscas_web",
                            )
                        result_by_index[index] = {
                            "query": query,
                            "provider": "duckduckgo",
                            "status": "ERROR",
                            "error": str(exc),
                            "returned": 0,
                            "accepted": 0,
                            "hits": [],
                        }

        results = [result_by_index[index] for index in range(len(requested))]
        return AgentBulkSearchResponse.model_validate(
            {"results": results}
        ).model_dump(mode="json")

    return StructuredTool.from_function(
        func=executar_buscas_web,
        args_schema=AgentBulkSearchArgs,
        name="executar_buscas_web",
        description=(
            "Executa em lote todas as consultas web do plano aprovado no DuckDuckGo, "
            "sem criar, renomear ou omitir consultas. A meta de corpus nao interrompe "
            "buscas aprovadas; SKIPPED e reservado a guardrails de integridade do plano."
        ),
        return_direct=False,
    )


def make_bulk_video_search_tool(
    *,
    sink: SearchSink | None = None,
    context: SearchContextResolver | None = None,
    providers: tuple[str, ...] = ("duckduckgo",),
    observer: SearchObserver | None = None,
) -> StructuredTool:
    def executar_buscas_videos(queries: list[str]) -> dict[str, Any]:
        """Busca vídeos em paralelo; sink/observer permanecem sequenciais."""
        settings = get_settings()
        requested = list(queries)
        result_by_index: dict[int, dict[str, Any]] = {}
        active: list[dict[str, Any]] = []

        for index, query in enumerate(requested):
            limit, window_start, window_end, _purpose = _query_options(context, query, 5)
            active.append(
                {
                    "index": index,
                    "query": query,
                    "limit": limit,
                    "window_start": window_start,
                    "window_end": window_end,
                }
            )

        if active:
            workers = (
                min(len(active), max(1, int(settings.search_parallel_video_workers)))
                if settings.search_parallel_enabled
                else 1
            )
            futures: dict[int, Future] = {}

            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="media-video",
            ) as executor:
                for item in active:
                    query = str(item["query"])
                    if observer is not None:
                        observer.query_started(query=query, tool_name="executar_buscas_videos")
                        if "duckduckgo" in providers:
                            observer.provider_attempted(
                                query=query,
                                provider="duckduckgo",
                                tool_name="executar_buscas_videos",
                            )

                    futures[int(item["index"])] = executor.submit(
                        _search_videos,
                        query,
                        max_results=int(item["limit"]),
                        providers=providers,
                        window_start=item["window_start"],
                        window_end=item["window_end"],
                        observer=None,
                        tool_name="executar_buscas_videos",
                        raise_unavailable=True,
                    )

                for item in active:
                    index = int(item["index"])
                    query = str(item["query"])
                    try:
                        provider, raw_rows = futures[index].result()
                        accepted_rows = sink(raw_rows, provider, query) if sink else raw_rows
                        if observer is not None:
                            observer.provider_result(
                                query=query,
                                provider=provider,
                                tool_name="executar_buscas_videos",
                                returned=len(raw_rows),
                                accepted=len(accepted_rows),
                            )
                            observer.query_finished(
                                query=query,
                                tool_name="executar_buscas_videos",
                            )
                        result_by_index[index] = {
                            "query": query,
                            "provider": provider,
                            "status": "OK" if accepted_rows else "NO_RESULTS",
                            "returned": len(raw_rows),
                            "accepted": len(accepted_rows),
                            "hits": [],
                        }
                    except Exception as exc:
                        if observer is not None:
                            observer.provider_error(
                                query=query,
                                provider="duckduckgo",
                                tool_name="executar_buscas_videos",
                                error=str(exc),
                            )
                            observer.query_finished(
                                query=query,
                                tool_name="executar_buscas_videos",
                            )
                        result_by_index[index] = {
                            "query": query,
                            "provider": "duckduckgo",
                            "status": "ERROR",
                            "error": str(exc),
                            "returned": 0,
                            "accepted": 0,
                            "hits": [],
                        }

        results = [result_by_index[index] for index in range(len(requested))]
        return AgentBulkSearchResponse.model_validate(
            {"results": results}
        ).model_dump(mode="json")

    return StructuredTool.from_function(
        func=executar_buscas_videos,
        args_schema=AgentBulkSearchArgs,
        name="executar_buscas_videos",
        description=(
            "Executa em lote todas as consultas de video do plano aprovado no "
            "DuckDuckGo Videos, sem criar, renomear ou omitir consultas."
        ),
        return_direct=False,
    )

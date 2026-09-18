"""Orquestração da coleta obrigatória executada pelo ReportAgent.

Regra estrutural: QUALQUER pesquisa web/vídeo é feita pelo agente. Este módulo
NÃO acessa provedores nem a cadeia de busca diretamente; ele monta o plano de
consultas (a metodologia), entrega as tools ``pesquisar_internet`` /
``pesquisar_videos`` ao ``ReportAgent`` (tarefa ``collector``) e concentra a
persistência determinística (guard, janela, dedup, proveniência e estatísticas)
nos *sinks* fornecidos às tools.

O acesso a provedores vive exclusivamente em ``app.tools.search``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.database import SessionLocal
from app.models import MediaItem, Project, SearchQuery
from app.schemas import CollectorExecutionResponse
from app.services.collection.common import (
    _valid_search_window,
    media_window,
    query_window,
)
from app.services.collection.persist import persist_video_rows, persist_web_rows
from app.services.collection.youtube_helpers import youtube_tasks_for_execution
from app.tools import build_agent_tools
from app.tools.search import (
    reset_tavily_circuit_breaker,
    search_providers_available,
    tavily_circuit_breaker_snapshot,
)


class CollectionState:
    """Estado compartilhado entre os sinks e o chamador da coleta."""

    def __init__(
        self,
        *,
        project_id: int,
        counters: dict[str, int],
        expect_queries: int,
        cancel_check: Callable[[], None] | None = None,
        progress_detail: Callable[[str], None] | None = None,
    ) -> None:
        self.project_id = project_id
        self.counters = counters
        self.expect_queries = expect_queries
        self.cancel_check = cancel_check
        self.progress_detail = progress_detail
        self.invoked = False
        self.cancelled = False
        self.cancel_exc: Exception | None = None
        self.unavailable: str | None = None
        self.added = 0
        self.errors: list[str] = []
        self.attempted: set[str] = set()
        self.resolved: set[str] = set()


def web_queries_pending(project_id: int) -> list[str]:
    """Snapshot das consultas web pendentes, respeitando o teto configurável."""
    settings = get_settings()
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(SearchQuery)
            .where(SearchQuery.project_id == project_id, SearchQuery.executed_at.is_(None))
            .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
        ).all()
        return [row.query for row in rows[: max(1, settings.max_search_queries)]]
    finally:
        session.close()


def video_queries_pending(project_id: int) -> list[str]:
    """Snapshot das tarefas de vídeo (canais prioritários primeiro)."""
    session = SessionLocal()
    try:
        project = session.get(Project, project_id)
        if not project:
            return []
        return [task.query for task in youtube_tasks_for_execution(project)]
    finally:
        session.close()


def _check_cancel(state: CollectionState) -> bool:
    """Devolve ``False`` (registrando o motivo) quando o cancelamento foi pedido.

    Não propaga a exceção: ela viajaria pela execução da tool e seria convertida
    em observação de erro pelo agente. O chamador re-levanta ao final.
    """
    if state.cancel_check is None:
        return True
    try:
        state.cancel_check()
    except Exception as exc:
        state.cancelled = True
        state.cancel_exc = exc
        return False
    return True


def _existing_items(session: Session, project_id: int) -> dict[str, MediaItem]:
    return {
        item.canonical_url: item
        for item in session.scalars(
            select(MediaItem).where(MediaItem.project_id == project_id)
        ).all()
    }


def _make_web_context(project: Project, plan: dict[str, SearchQuery]):
    settings = get_settings()

    def context(query: str) -> dict[str, Any]:
        options: dict[str, Any] = {"max_results": max(1, settings.max_results_per_query)}
        entry = plan.get(query)
        if entry is not None:
            start, end = query_window(project, entry)
            options["window_start"] = start.isoformat() if start else None
            options["window_end"] = end.isoformat() if end else None
            options["purpose"] = entry.purpose
        return options

    return context


def _make_video_context(project: Project):
    settings = get_settings()
    start, end = media_window(project)

    def context(query: str) -> dict[str, Any]:
        return {
            "max_results": max(1, settings.max_results_per_query),
            "window_start": start.isoformat() if start else None,
            "window_end": end.isoformat() if end else None,
        }

    return context


def _make_web_attempt(
    state: CollectionState,
    session: Session,
    plan: dict[str, SearchQuery],
):
    """Registra que a tool iniciou de fato a consulta planejada.

    Marca ``SearchQuery.executed_at`` no primeiro início, mesmo quando a busca
    não retorna resultado algum. Isso separa "tentada" de "com resultado
    utilizável" e impede que uma consulta sem resultados seja reexecutada ou
    contada como não executada.
    """
    counters = state.counters
    progress = state.progress_detail

    def attempt(query: str) -> None:
        state.invoked = True
        state.attempted.add(query)
        counters["queries_attempted"] = counters.get("queries_attempted", 0) + 1
        entry = plan.get(query)
        if entry is not None and entry.executed_at is None:
            entry.executed_at = datetime.now(timezone.utc)
            try:
                session.flush()
            except Exception as exc:
                state.errors.append(f"{query}: falha ao registrar execução ({exc})")
        if progress:
            progress(f"Executando consulta: {query[:90]}")

    return attempt


def _make_video_attempt(state: CollectionState):
    counters = state.counters

    def attempt(query: str) -> None:
        state.invoked = True
        state.attempted.add(query)
        counters["tasks_attempted"] = counters.get("tasks_attempted", 0) + 1

    return attempt


def _make_web_sink(
    state: CollectionState,
    session: Session,
    project: Project,
    plan: dict[str, SearchQuery],
    existing_items: dict[str, MediaItem],
):
    counters = state.counters
    settings = get_settings()
    global_limit = max(1, counters.get("global_result_limit", settings.max_search_results))
    per_query_cap = max(1, settings.max_results_per_query)
    progress = state.progress_detail

    def sink(rows: list[dict[str, Any]], provider: str, query: str) -> list[dict[str, Any]]:
        state.invoked = True
        state.attempted.add(query)
        if not _check_cancel(state):
            return []

        entry = plan.get(query)
        if entry is None:
            state.errors.append(f"{query}: consulta fora do plano aprovado")
            return []

        if state.added >= global_limit:
            counters["global_limit_reached"] = 1
            if progress:
                progress(
                    f"Limite global de {global_limit} item(ns) atingido; consulta ignorada"
                )
            return []

        remaining = global_limit - state.added
        query_limit = min(per_query_cap, max(1, remaining))
        start, end = query_window(project, entry)
        has_window = _valid_search_window(start, end)

        local, accepted_rows = persist_web_rows(
            session,
            project,
            entry,
            existing_items,
            rows,
            has_window=has_window,
            start=start,
            end=end,
            max_accepted=query_limit,
            progress_detail=progress,
        )

        if provider == "duckduckgo":
            counters["duckduckgo_queries"] += 1
            counters["duckduckgo_results"] += len(rows)
            counters["duckduckgo_added"] += int(local.get("added", 0))
            counters["duckduckgo_rejected"] += int(local.get("rejected", 0))
        elif provider == "tavily":
            counters["tavily_queries"] += 1
            counters["tavily_results"] += len(rows)
            counters["tavily_added"] += int(local.get("added", 0))
            counters["tavily_rejected"] += int(local.get("rejected", 0))

        state.added += int(local.get("added", 0))
        if provider in {"duckduckgo", "tavily"}:
            counters["queries_successful"] += 1
        else:
            counters["failed_queries"] += 1
        return accepted_rows

    return sink


def _make_video_sink(
    state: CollectionState,
    session: Session,
    project: Project,
    plan: dict[str, Any],
    existing_items: dict[str, MediaItem],
):
    counters = state.counters
    settings = get_settings()
    total_limit = max(1, settings.max_youtube_results_total)
    per_task_cap = max(1, settings.max_youtube_results_per_task)
    start, end = media_window(project)
    has_window = _valid_search_window(start, end)
    progress = state.progress_detail

    def sink(rows: list[dict[str, Any]], provider: str, query: str) -> list[dict[str, Any]]:
        state.invoked = True
        state.attempted.add(query)
        if not _check_cancel(state):
            return []

        task = plan.get(query)
        if task is None:
            state.errors.append(f"{query}: tarefa fora do plano aprovado")
            return []

        if state.added >= total_limit:
            return []

        is_priority_task = task.is_priority
        task_limit = min(
            1 if is_priority_task else per_task_cap,
            max(1, total_limit - state.added),
        )

        if provider == "duckduckgo":
            counters["duckduckgo_attempts"] += 1
        elif provider == "tavily":
            counters["tavily_attempts"] += 1
        else:
            return []

        local, accepted_rows = persist_video_rows(
            session,
            project,
            task,
            existing_items,
            rows,
            has_window=has_window,
            start=start,
            end=end,
            max_accepted=task_limit,
            enforce_priority_channel=(provider == "duckduckgo"),
            progress_detail=progress,
        )
        if provider == "duckduckgo":
            counters["duckduckgo_added"] += int(local.get("added", 0))
        else:
            counters["tavily_added"] += int(local.get("added", 0))
        state.added += int(local.get("added", 0))
        if int(local.get("added", 0)) > 0 or int(local.get("duplicates", 0)) > 0:
            state.resolved.add(query)
        return accepted_rows

    return sink


def _mark_web_missed(state: CollectionState, planned: list[str]) -> None:
    missed = [query for query in planned if query not in state.attempted]
    if not missed:
        return
    state.counters["failed_queries"] = state.counters.get("failed_queries", 0) + len(missed)
    state.errors.append(f"web: {len(missed)} consulta(s) do plano não executada(s)")


def run_agent_collection(
    *,
    project_id: int,
    web_queries: list[str],
    web_counters: dict[str, int] | None = None,
    web_progress: Callable[[str], None] | None = None,
    video_queries: list[str] | None = None,
    video_counters: dict[str, int] | None = None,
    video_progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[CollectionState, CollectionState | None]:
    """Executa o plano de coleta pelo agente e devolve os estados consolidados."""
    reset_tavily_circuit_breaker()
    web_state = CollectionState(
        project_id=project_id,
        counters=web_counters if web_counters is not None else {},
        expect_queries=len(web_queries),
        cancel_check=cancel_check,
        progress_detail=web_progress,
    )
    video_state: CollectionState | None = None
    if video_queries is not None:
        video_state = CollectionState(
            project_id=project_id,
            counters=video_counters if video_counters is not None else {},
            expect_queries=len(video_queries),
            cancel_check=cancel_check,
            progress_detail=video_progress,
        )

    if not web_queries and not (video_state and video_state.expect_queries):
        return web_state, video_state

    if not search_providers_available():
        message = (
            "Nenhum provedor de pesquisa está disponível. Instale `ddgs` "
            "ou configure TAVILY_API_KEY."
        )
        if web_queries:
            web_state.unavailable = message
        if video_state and video_state.expect_queries:
            video_state.unavailable = message
        return web_state, video_state

    session = SessionLocal()
    try:
        project = session.get(Project, project_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado")

        existing_items = _existing_items(session, project_id)

        web_plan: dict[str, SearchQuery] = {}
        web_context = None
        web_sink = None
        web_attempt = None
        tools = []
        if web_queries:
            pending = session.scalars(
                select(SearchQuery).where(
                    SearchQuery.project_id == project_id,
                    SearchQuery.executed_at.is_(None),
                )
            ).all()
            web_plan = {row.query: row for row in pending}
            web_state.counters["queries_total"] = max(
                web_state.counters.get("queries_total", 0), len(web_queries)
            )
            web_context = _make_web_context(project, web_plan)
            web_attempt = _make_web_attempt(web_state, session, web_plan)
            web_sink = _make_web_sink(web_state, session, project, web_plan, existing_items)

        video_plan: dict[str, Any] = {}
        video_context = None
        video_sink = None
        video_attempt = None
        if video_state is not None and video_state.expect_queries:
            video_plan = {task.query: task for task in youtube_tasks_for_execution(project)}
            video_state.counters["tasks_total"] = max(
                video_state.counters.get("tasks_total", 0), len(video_queries or [])
            )
            video_context = _make_video_context(project)
            video_attempt = _make_video_attempt(video_state)
            video_sink = _make_video_sink(
                video_state, session, project, video_plan, existing_items
            )

        tools = build_agent_tools(
            enable_web=bool(web_sink),
            enable_video=bool(video_sink),
            web_sink=web_sink,
            video_sink=video_sink,
            web_context=web_context,
            video_context=video_context,
            web_on_attempt=web_attempt,
            video_on_attempt=video_attempt,
        )

        payload: dict[str, Any] = {
            "project_id": project_id,
            "topic": project.topic,
            "web_queries": web_queries,
        }
        if video_state is not None and video_state.expect_queries:
            payload["youtube_queries"] = video_queries

        total_queries = len(web_queries) + len(video_queries or [])
        try:
            run_collector_agent(
                payload=payload,
                tools=tools,
                max_tool_rounds=max(6, min(60, total_queries + 3)),
            )
        except Exception as exc:
            if web_state.cancelled or (video_state is not None and video_state.cancelled):
                raise (web_state.cancel_exc or video_state.cancel_exc)  # type: ignore[misc]
            message = f"Agente de coleta indisponível: {str(exc)[:1000]}"
            if web_queries and not web_state.invoked:
                web_state.unavailable = message
            if video_state is not None and video_state.expect_queries and not video_state.invoked:
                video_state.unavailable = message

        if web_queries and not web_state.invoked and not web_state.unavailable:
            web_state.unavailable = "O agente de coleta não executou a coleta web planejada"
        if (
            video_state is not None
            and video_state.expect_queries
            and not video_state.invoked
            and not video_state.unavailable
        ):
            video_state.unavailable = "O agente de coleta não executou a coleta de vídeos planejada"

        _mark_web_missed(web_state, web_queries)
        if video_state is not None and video_state.expect_queries:
            planned_video = list(video_queries or [])
            resolved = len([query for query in planned_video if query in video_state.resolved])
            video_state.counters["tasks_resolved"] = resolved
            video_state.counters["tasks_failed"] = max(0, len(planned_video) - resolved)

        breaker = tavily_circuit_breaker_snapshot()
        for state in (web_state, video_state):
            if state is None:
                continue
            state.counters["tavily_hard_failures"] = int(breaker["hard_failures"])
            state.counters["tavily_circuit_breaker_trips"] = int(breaker["trips"])

        session.commit()
    finally:
        session.close()

    return web_state, video_state


def run_collector_agent(
    *,
    payload: dict[str, Any],
    tools: list,
    max_tool_rounds: int = 6,
) -> dict[str, Any]:
    return get_report_agent().run(
        task="collector",
        payload=payload,
        response_model=CollectorExecutionResponse,
        tools=tools,
        max_tool_rounds=max_tool_rounds,
        max_output_tokens=2000,
    )

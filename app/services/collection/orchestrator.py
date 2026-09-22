"""Mandatory collection orchestration executed through ReportAgent tools.

Structural invariant: services never call DuckDuckGo or tool.invoke().
The ReportAgent invokes the bulk tools; this module only supplies approved
queries, deterministic context, sinks, guards, budgets and audit observers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import current_run_id
from app.database import SessionLocal
from app.models import MediaItem, Project, SearchCall, SearchQuery
from app.schemas import CollectorExecutionResponse
from app.services.collection.common import (
    _valid_search_window,
    media_window,
    query_window,
)
from app.services.collection.persist import persist_video_rows, persist_web_rows
from app.services.collection.youtube_helpers import youtube_tasks_for_execution
from app.tools import SearchObserver, build_agent_tools
from app.tools.search import search_providers_available


class CollectionState:
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
        self.added_by_purpose: dict[str, int] = {}
        self.errors: list[str] = []
        self.attempted: set[str] = set()
        self.resolved: set[str] = set()


def _query_budget_for_purpose(settings, purpose: str) -> int:
    return {
        "MEDIA_REPERCUSSION": settings.max_media_queries,
        "FACT_DISCOVERY": settings.max_fact_queries,
        "OFFICIAL_FACT": settings.max_official_queries,
        "NOMINAL_FOLLOWUP": settings.max_nominal_queries,
    }.get(purpose, settings.max_search_queries)


def web_queries_pending(project_id: int) -> list[str]:
    """Return pending queries with independent per-purpose query budgets."""
    settings = get_settings()
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(SearchQuery)
            .where(
                SearchQuery.project_id == project_id,
                SearchQuery.executed_at.is_(None),
            )
            .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
        ).all()
        selected: list[str] = []
        counts: dict[str, int] = {}
        for row in rows:
            purpose = str(row.purpose or "MEDIA_REPERCUSSION")
            kind = str(row.kind or "")
            if kind == "fact_inventory_month":
                budget_key = "FACT_INVENTORY_MONTH"
                limit = max(0, int(settings.max_annual_event_inventory_queries))
            elif kind == "official_operation_inventory":
                budget_key = "OFFICIAL_OPERATION_INVENTORY"
                limit = max(0, int(settings.max_official_inventory_queries))
            else:
                budget_key = purpose
                limit = max(0, int(_query_budget_for_purpose(settings, purpose)))
            if counts.get(budget_key, 0) >= limit:
                continue
            selected.append(row.query)
            counts[budget_key] = counts.get(budget_key, 0) + 1
        return selected
    finally:
        session.close()


def video_queries_pending(project_id: int) -> list[str]:
    session = SessionLocal()
    try:
        project = session.get(Project, project_id)
        if not project:
            return []
        return [task.query for task in youtube_tasks_for_execution(project)]
    finally:
        session.close()


def _check_cancel(state: CollectionState) -> bool:
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


def _make_web_context(
    project: Project,
    plan: dict[str, SearchQuery],
    state: CollectionState,
):
    """Resolve provider options without stopping approved queries by corpus target.

    The only pre-provider skip is an unapproved query. Targets and later
    eligibility never suppress an approved external search.
    """
    settings = get_settings()

    def context(query: str) -> dict[str, Any]:
        entry = plan.get(query)
        if entry is None:
            return {
                "skip": True,
                "skip_reason": "query outside approved plan",
                "max_results": 1,
            }

        if str(entry.purpose or "") == "OFFICIAL_FACT":
            per_query = settings.max_official_search_results
        elif str(entry.purpose or "") == "FACT_DISCOVERY":
            per_query = settings.max_fact_search_results
        elif str(entry.purpose or "") == "NOMINAL_FOLLOWUP":
            per_query = settings.max_nominal_search_results
        else:
            per_query = (
                settings.max_priority_results_per_query
                if entry.kind == "media_portal"
                else settings.max_results_per_query
            )
        # Do not pre-filter at the provider by date. The query text keeps the
        # requested temporal context, while every returned hit is preserved and
        # the real window decision happens later in validation/fact extraction.
        return {
            "max_results": max(1, int(per_query)),
            "window_start": None,
            "window_end": None,
            "purpose": str(entry.purpose or "MEDIA_REPERCUSSION"),
        }

    return context

def _make_video_context(project: Project):
    settings = get_settings()

    def context(query: str) -> dict[str, Any]:
        # Same raw-first rule as web: preserve provider hits, validate dates later.
        return {
            "max_results": max(1, settings.max_youtube_results_per_task),
            "window_start": None,
            "window_end": None,
        }

    return context


class SearchAuditObserver(SearchObserver):
    def __init__(
        self,
        *,
        session: Session,
        project_id: int,
        plan: dict[str, Any],
        state: CollectionState,
        count_key: str,
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.run_id = current_run_id()
        self.plan = plan
        self.state = state
        self.count_key = count_key
        self._open: dict[tuple[str, str, str], SearchCall] = {}
        self._started: dict[tuple[str, str, str], float] = {}
        self._attempts: dict[str, list[str]] = {}
        self._returned: dict[str, int] = {}
        self._accepted: dict[str, int] = {}
        self._errors: dict[str, list[str]] = {}

    def _key(self, query: str, provider: str, tool_name: str) -> tuple[str, str, str]:
        return (query, provider, tool_name)

    def query_started(self, *, query: str, tool_name: str) -> None:
        self.state.invoked = True
        self.state.attempted.add(query)
        self.state.counters[self.count_key] = self.state.counters.get(self.count_key, 0) + 1
        entry = self.plan.get(query)
        if isinstance(entry, SearchQuery) and entry.executed_at is None:
            entry.executed_at = datetime.now(timezone.utc)
            entry.execution_status = "ATTEMPTED"
            try:
                self.session.flush()
            except Exception as exc:
                self.state.errors.append(f"{query}: audit start failed ({exc})")
        if self.state.progress_detail:
            self.state.progress_detail(f"Executando consulta: {query[:90]}")

    def provider_attempted(self, *, query: str, provider: str, tool_name: str) -> None:
        key = self._key(query, provider, tool_name)
        entry = self.plan.get(query)
        call = SearchCall(
            project_id=self.project_id,
            run_id=self.run_id,
            search_query_id=getattr(entry, "id", None),
            tool_name=tool_name,
            provider=provider,
            query=query,
            started_at=datetime.now(timezone.utc),
        )
        self.session.add(call)
        self._open[key] = call
        self._started[key] = time.monotonic()
        self._attempts.setdefault(query, [])
        if provider not in self._attempts[query]:
            self._attempts[query].append(provider)

    def provider_result(
        self,
        *,
        query: str,
        provider: str,
        tool_name: str,
        returned: int,
        accepted: int,
    ) -> None:
        key = self._key(query, provider, tool_name)
        call = self._open.pop(key, None)
        if call is not None:
            call.finished_at = datetime.now(timezone.utc)
            call.success = True
            call.results_returned = int(returned)
            call.results_accepted = int(accepted)
            call.latency_ms = self._latency(key)
        self._returned[query] = self._returned.get(query, 0) + int(returned)
        self._accepted[query] = self._accepted.get(query, 0) + int(accepted)

    def provider_error(
        self,
        *,
        query: str,
        provider: str,
        tool_name: str,
        error: str,
    ) -> None:
        key = self._key(query, provider, tool_name)
        call = self._open.pop(key, None)
        if call is not None:
            call.finished_at = datetime.now(timezone.utc)
            call.success = False
            call.error = (error or "")[:2000] or None
            call.latency_ms = self._latency(key)
        self._errors.setdefault(query, []).append(f"{provider}: {error}"[:2000])

    def query_finished(self, *, query: str, tool_name: str) -> None:
        entry = self.plan.get(query)
        if not isinstance(entry, SearchQuery):
            return
        returned = self._returned.get(query, 0)
        accepted = self._accepted.get(query, 0)
        providers = self._attempts.get(query, [])
        entry.results_returned = max(int(entry.results_returned or 0), returned)
        entry.results_accepted = max(int(entry.results_accepted or 0), accepted)
        entry.providers_attempted = providers
        errors = self._errors.get(query, [])
        if accepted > 0:
            entry.execution_status = "SUCCEEDED"
        elif returned > 0:
            # A pesquisa retornou hits, mas todos ficaram sinalizados tecnicamente.
            # Os hits continuam preservados em search_hits para analise posterior.
            entry.execution_status = "SUCCEEDED_FLAGGED"
        elif errors and returned == 0:
            entry.execution_status = "FAILED"
            entry.execution_error = "; ".join(errors)[:2000]
        elif providers:
            entry.execution_status = "NO_RESULTS"
        else:
            entry.execution_status = "SKIPPED"

    def _latency(self, key: tuple[str, str, str]) -> int:
        started = self._started.pop(key, None)
        if started is None:
            return 0
        return max(0, int((time.monotonic() - started) * 1000))


def _purpose_counter_key(purpose: str) -> str:
    return {
        "MEDIA_REPERCUSSION": "media_added",
        "FACT_DISCOVERY": "fact_added",
        "OFFICIAL_FACT": "official_added",
        "NOMINAL_FOLLOWUP": "nominal_added",
    }.get(purpose, "other_added")


def _make_web_sink(
    state: CollectionState,
    session: Session,
    project: Project,
    plan: dict[str, SearchQuery],
    existing_items: dict[str, MediaItem],
):
    counters = state.counters
    settings = get_settings()
    progress = state.progress_detail

    def sink(rows: list[dict[str, Any]], provider: str, query: str) -> list[dict[str, Any]]:
        state.invoked = True
        state.attempted.add(query)
        if not _check_cancel(state):
            return []

        entry = plan.get(query)
        if entry is None:
            state.errors.append(f"{query}: query outside approved plan")
            return []

        purpose = str(entry.purpose or "MEDIA_REPERCUSSION")
        start, end = query_window(project, entry)
        has_window = _valid_search_window(start, end)

        # Every row returned by the provider is persisted. max_accepted only
        # controls which rows resolve the provider fallback, never storage.
        per_query_cap = (
            settings.max_priority_results_per_query
            if entry.kind == "media_portal"
            else settings.max_results_per_query
        )
        local, usable_rows = persist_web_rows(
            session,
            project,
            entry,
            existing_items,
            rows,
            has_window=has_window,
            start=start,
            end=end,
            max_accepted=max(1, int(per_query_cap)),
            progress_detail=progress,
        )

        counters["raw_hits"] = counters.get("raw_hits", 0) + int(local.get("persisted", 0))
        counters["flagged_hits"] = counters.get("flagged_hits", 0) + int(local.get("flagged", 0))
        counters["invalid_hits"] = counters.get("invalid_hits", 0) + int(local.get("invalid", 0))

        if provider == "duckduckgo":
            counters["duckduckgo_queries"] = counters.get("duckduckgo_queries", 0) + 1
            counters["duckduckgo_results"] = counters.get("duckduckgo_results", 0) + len(rows)
            counters["duckduckgo_added"] = counters.get("duckduckgo_added", 0) + int(local.get("added", 0))
            counters["duckduckgo_rejected"] = counters.get("duckduckgo_rejected", 0)

        added = int(local.get("added", 0))
        state.added += added
        state.added_by_purpose[purpose] = state.added_by_purpose.get(purpose, 0) + added
        counters[_purpose_counter_key(purpose)] = state.added_by_purpose[purpose]

        # Target is informational only. It NEVER stops collection.
        if (
            purpose == "MEDIA_REPERCUSSION"
            and state.added_by_purpose[purpose] >= settings.target_media_items
        ):
            counters["media_target_reached"] = 1

        if provider == "duckduckgo":
            counters["queries_successful"] = counters.get("queries_successful", 0) + 1
        else:
            counters["failed_queries"] = counters.get("failed_queries", 0) + 1
        return usable_rows

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
            state.errors.append(f"{query}: task outside approved plan")
            return []

        if provider == "duckduckgo":
            counters["duckduckgo_attempts"] = counters.get("duckduckgo_attempts", 0) + 1
        else:
            return []

        local, usable_rows = persist_video_rows(
            session,
            project,
            task,
            existing_items,
            rows,
            has_window=has_window,
            start=start,
            end=end,
            max_accepted=per_task_cap,
            enforce_priority_channel=True,
            progress_detail=progress,
        )
        counters["raw_hits"] = counters.get("raw_hits", 0) + int(local.get("persisted", 0))
        counters["flagged_hits"] = counters.get("flagged_hits", 0) + int(local.get("flagged", 0))
        if provider == "duckduckgo":
            counters["duckduckgo_added"] = counters.get("duckduckgo_added", 0) + int(local.get("added", 0))
        state.added += int(local.get("added", 0))
        if usable_rows:
            state.resolved.add(query)
        return usable_rows

    return sink

def _mark_web_missed(state: CollectionState, planned: list[str]) -> None:
    missed = [query for query in planned if query not in state.attempted]
    if not missed:
        return
    state.counters["failed_queries"] = state.counters.get("failed_queries", 0) + len(missed)
    state.errors.append(f"web: {len(missed)} planned query/queries not executed")


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
        message = "No search provider is available. Install ddgs."
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
        web_observer = None
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
            web_context = _make_web_context(project, web_plan, web_state)
            web_observer = SearchAuditObserver(
                session=session,
                project_id=project_id,
                plan=web_plan,
                state=web_state,
                count_key="queries_attempted",
            )
            web_sink = _make_web_sink(web_state, session, project, web_plan, existing_items)

        video_plan: dict[str, Any] = {}
        video_context = None
        video_sink = None
        video_observer = None
        if video_state is not None and video_state.expect_queries:
            video_plan = {task.query: task for task in youtube_tasks_for_execution(project)}
            video_state.counters["tasks_total"] = max(
                video_state.counters.get("tasks_total", 0), len(video_queries or [])
            )
            video_context = _make_video_context(project)
            video_observer = SearchAuditObserver(
                session=session,
                project_id=project_id,
                plan=video_plan,
                state=video_state,
                count_key="tasks_attempted",
            )
            video_sink = _make_video_sink(
                video_state,
                session,
                project,
                video_plan,
                existing_items,
            )

        tools = build_agent_tools(
            enable_web=bool(web_sink),
            enable_video=bool(video_sink),
            web_sink=web_sink,
            video_sink=video_sink,
            web_context=web_context,
            video_context=video_context,
            web_observer=web_observer,
            video_observer=video_observer,
            bulk=True,
        )

        payload: dict[str, Any] = {
            "project_id": project_id,
            "topic": project.topic,
            "web_queries": web_queries,
        }
        if video_state is not None and video_state.expect_queries:
            payload["youtube_queries"] = video_queries

        try:
            run_collector_agent(
                payload=payload,
                tools=tools,
                max_tool_rounds=max(3, get_settings().max_agent_tool_rounds),
            )
        except Exception as exc:
            if web_state.cancelled or (video_state is not None and video_state.cancelled):
                raise (web_state.cancel_exc or video_state.cancel_exc)  # type: ignore[misc]
            message = f"Collector agent unavailable: {str(exc)[:1000]}"
            if web_queries and not web_state.invoked:
                web_state.unavailable = message
            if video_state is not None and video_state.expect_queries and not video_state.invoked:
                video_state.unavailable = message

        if web_queries and not web_state.invoked and not web_state.unavailable:
            web_state.unavailable = "Collector agent did not execute the approved web plan"
        if (
            video_state is not None
            and video_state.expect_queries
            and not video_state.invoked
            and not video_state.unavailable
        ):
            video_state.unavailable = "Collector agent did not execute the approved video plan"

        _mark_web_missed(web_state, web_queries)
        if video_state is not None and video_state.expect_queries:
            planned_video = list(video_queries or [])
            resolved = len([query for query in planned_video if query in video_state.resolved])
            video_state.counters["tasks_resolved"] = resolved
            video_state.counters["tasks_failed"] = max(0, len(planned_video) - resolved)

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

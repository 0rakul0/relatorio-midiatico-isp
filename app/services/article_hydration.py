"""Hydrate consolidated MediaItems through a ReportAgent tool call."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.models import MediaItem, Project
from app.schemas import HydrationExecutionResponse
from app.services.collection.youtube_helpers import is_youtube_url
from app.tools import build_agent_tools


def _needs_hydration(item: MediaItem, min_existing_chars: int) -> bool:
    if not (item.url or "").startswith(("http://", "https://")):
        return False
    if is_youtube_url(item.url):
        return False
    content = (item.content or "").strip()
    snippet = (item.snippet or "").strip()
    # During light collection MediaItem.content may intentionally equal snippet.
    return len(content) < min_existing_chars or (snippet and content == snippet)


def hydrate_media_items(
    db: Session,
    project: Project,
    *,
    items: Iterable[MediaItem] | None = None,
    purposes: set[str] | None = None,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict[str, int | str]:
    settings = get_settings()
    if not settings.article_fetch_enabled:
        return {"requested": 0, "fetched": 0, "empty": 0, "errors": 0, "status": "DISABLED"}

    candidates = list(items) if items is not None else db.scalars(
        select(MediaItem).where(MediaItem.project_id == project.id).order_by(MediaItem.id.asc())
    ).all()

    if purposes:
        candidates = [
            item for item in candidates
            if purposes.intersection(set(item.discovery_purposes or []))
        ]

    candidates = [
        item for item in candidates
        if _needs_hydration(item, settings.article_fetch_min_existing_chars)
    ][: settings.article_fetch_max_items]

    if not candidates:
        return {"requested": 0, "fetched": 0, "empty": 0, "errors": 0, "status": "COMPLETED"}

    if cancel_check:
        cancel_check()

    by_url = {item.url: item for item in candidates}
    counters = {"requested": len(candidates), "fetched": 0, "empty": 0, "errors": 0}

    def sink(url: str, content: str | None, status: str, error: str | None) -> None:
        item = by_url.get(url)
        if item is None:
            return
        if status == "FETCHED" and content:
            item.content = content
            counters["fetched"] += 1
        elif status == "ERROR":
            counters["errors"] += 1
        else:
            counters["empty"] += 1
        if progress_detail:
            progress_detail(
                f"Hidratacao: {counters['fetched']} obtida(s), "
                f"{counters['empty']} vazia/bloqueada(s), {counters['errors']} erro(s)"
            )

    tools = build_agent_tools(enable_article_fetch=True, article_sink=sink)
    try:
        get_report_agent().run(
            task="article_hydrator",
            payload={"project_id": project.id, "urls": list(by_url)},
            response_model=HydrationExecutionResponse,
            tools=tools,
            max_tool_rounds=max(2, settings.max_agent_tool_rounds),
            max_output_tokens=800,
        )
    except Exception as exc:
        db.commit()  # preserve any pages fetched before a partial failure
        return {**counters, "status": "PARTIAL", "error": str(exc)[:1000]}

    db.commit()
    return {**counters, "status": "COMPLETED" if counters["errors"] == 0 else "PARTIAL"}

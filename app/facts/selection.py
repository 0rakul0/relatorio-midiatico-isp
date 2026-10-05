from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MediaItem, Project, SearchQuery
from app.topic_profile import normalized_text


FACT_PURPOSES = {"FACT_DISCOVERY", "OFFICIAL_FACT", "NOMINAL_FOLLOWUP"}


def query_purpose_map(db: Session, project_id: int) -> dict[int, str]:
    rows = db.execute(
        select(SearchQuery.id, SearchQuery.purpose).where(
            SearchQuery.project_id == project_id
        )
    ).all()
    return {query_id: purpose for query_id, purpose in rows}


def is_annual_police_operation_project(project: Project) -> bool:
    if project.project_type != "EVENT_TOPIC":
        return False
    if not project.event_start or not project.event_end:
        return False
    if (project.event_end - project.event_start).days < 180:
        return False
    haystack = normalized_text(
        " ".join(
            [
                project.topic or "",
                str((project.topic_profile or {}).get("event_anchor") or ""),
            ]
        )
    )
    return "operac" in haystack and "polic" in haystack


def item_should_feed_fact_layer(
    item: MediaItem,
    purpose_by_query: dict[int, str],
    project: Project,
) -> bool:
    purposes = set(item.discovery_purposes or [])
    if item.query_id and purpose_by_query.get(item.query_id):
        purposes.add(purpose_by_query[item.query_id])
    if purposes.intersection(FACT_PURPOSES):
        return True

    if is_annual_police_operation_project(project) and item.status == "VALID":
        item_text = normalized_text(
            " ".join(
                [
                    item.title or "",
                    item.snippet or "",
                    (item.content or "")[:2000],
                ]
            )
        )
        if "operac" in item_text and ("polic" in item_text or "bope" in item_text):
            return True

    return (
        project.project_type == "EVENT_TOPIC"
        and item.search_source == "manual"
    )

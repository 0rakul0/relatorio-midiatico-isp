from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, SearchQuery
from app.search.guards import query_is_acceptable


def existing_queries(db: Session, project_id: int) -> set[str]:
    return set(
        db.scalars(
            select(SearchQuery.query).where(SearchQuery.project_id == project_id)
        ).all()
    )


def add_query(
    db: Session,
    project: Project,
    existing: set[str],
    *,
    query: str,
    kind: str,
    purpose: str,
    rationale: str,
    priority: int,
) -> SearchQuery | None:
    query = " ".join(query.split()).strip()
    if not query or query in existing:
        return None
    if not query_is_acceptable(project, query, purpose=purpose):
        return None

    row = SearchQuery(
        project_id=project.id,
        query=query,
        kind=kind,
        purpose=purpose,
        rationale=rationale,
        priority=priority,
    )
    db.add(row)
    existing.add(query)
    return row

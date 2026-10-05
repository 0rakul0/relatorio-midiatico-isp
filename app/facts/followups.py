from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FactEvent, Project, SearchQuery


def plan_nominal_followups(db: Session, project: Project) -> int:
    events = list(
        db.scalars(
            select(FactEvent).where(
                FactEvent.project_id == project.id,
                FactEvent.subject_name.is_not(None),
            )
        ).all()
    )
    existing = set(
        db.scalars(
            select(SearchQuery.query).where(SearchQuery.project_id == project.id)
        ).all()
    )
    created = 0
    for event in events:
        name = (event.subject_name or "").strip()
        if not name:
            continue
        locations = (project.topic_profile or {}).get("locations") or ["Rio de Janeiro"]
        location = event.city or locations[0]
        organization = event.institution or ""
        queries = [
            '"' + name + '"',
            ('"' + name + '" ' + organization).strip(),
            '"' + name + '" "' + location + '"',
        ]
        for query in queries:
            if query in existing:
                continue
            db.add(
                SearchQuery(
                    project_id=project.id,
                    query=query,
                    kind="nominal",
                    purpose="NOMINAL_FOLLOWUP",
                    rationale="Busca nominal de corroboradores para fato já identificado",
                    priority=1,
                )
            )
            existing.add(query)
            created += 1
    db.commit()
    return created

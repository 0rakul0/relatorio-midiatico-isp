from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FactAssertion, FactEvent, Project


def fact_event_exclusion_reason(project: Project, event: FactEvent) -> str | None:
    """Return the reason an event must stay out of the report's factual core."""
    event_day = event.event_date or event.death_date
    if not event_day:
        return "DATA_DO_FATO_NAO_CONFIRMADA"
    if project.event_start and event_day < project.event_start:
        return "FATO_ANTERIOR_A_JANELA_SOLICITADA"
    if project.event_end and event_day > project.event_end:
        return "FATO_POSTERIOR_A_JANELA_SOLICITADA"
    if event.primary_scope is False:
        return "FORA_DO_ESCOPO_TEMATICO"
    if event.resolution_status in {"SOURCE_CONFLICT", "NOT_FOUND_IN_SAMPLE"}:
        return "FATO_SEM_RESOLUCAO_CONFIAVEL"
    return None


def fact_events_for_report(db: Session, project_id: int) -> list[dict]:
    project = db.get(Project, project_id)
    events = db.scalars(
        select(FactEvent)
        .where(FactEvent.project_id == project_id)
        .order_by(
            FactEvent.event_date.asc().nullslast(),
            FactEvent.subject_name.asc().nullslast(),
            FactEvent.id.asc(),
        )
    ).all()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "operation_name": event.operation_name,
            "subject_name": event.subject_name,
            "subject_type": event.subject_type,
            "institution": event.institution,
            "rank_or_role": event.rank_or_role,
            "unit": event.unit,
            "professional_status": event.professional_status,
            "event_date": event.event_date.isoformat() if event.event_date else None,
            "death_date": event.death_date.isoformat() if event.death_date else None,
            "cause_category": event.cause_category,
            "cause": event.cause_description,
            "circumstance": event.circumstance,
            "address": event.address,
            "neighborhood": event.neighborhood,
            "city": event.city,
            "state": event.state,
            "death_place_name": event.death_place_name,
            "death_address": event.death_address,
            "death_neighborhood": event.death_neighborhood,
            "death_city": event.death_city,
            "death_state": event.death_state,
            "primary_scope": event.primary_scope,
            "resolution_status": event.resolution_status,
            "conflict_fields": event.conflict_fields or [],
            "count_timelines": (event.extra_attributes or {}).get("count_timelines") or {},
            "counts_are_time_varying": bool(
                (event.extra_attributes or {}).get("counts_are_time_varying")
            ),
            "death_count_latest_official": (
                event.extra_attributes or {}
            ).get("death_count_latest_official"),
            "death_count_latest_reported": (
                event.extra_attributes or {}
            ).get("death_count_latest_reported"),
            "report_exclusion_reason": (
                fact_event_exclusion_reason(project, event) if project else None
            ),
        }
        for event in events
    ]


def fact_events_for_main_report(db: Session, project_id: int) -> list[dict]:
    project = db.get(Project, project_id)
    if not project:
        return []
    return [
        event
        for event in fact_events_for_report(db, project_id)
        if not event["report_exclusion_reason"]
    ]


def fact_event_validation_summary(db: Session, project_id: int) -> dict:
    project = db.get(Project, project_id)
    if not project:
        return {
            "total": 0,
            "eligible": 0,
            "excluded": 0,
            "excluded_by_reason": {},
        }
    events = db.scalars(
        select(FactEvent).where(FactEvent.project_id == project_id)
    ).all()
    reasons: dict[str, int] = {}
    for event in events:
        reason = fact_event_exclusion_reason(project, event)
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "total": len(events),
        "eligible": len(events) - sum(reasons.values()),
        "excluded": sum(reasons.values()),
        "excluded_by_reason": reasons,
    }


def fact_assertions_for_report(
    db: Session,
    project_id: int,
    *,
    main_report_only: bool = False,
) -> list[dict]:
    rows = db.execute(
        select(FactEvent, FactAssertion)
        .join(FactAssertion, FactAssertion.event_id == FactEvent.id)
        .where(FactEvent.project_id == project_id)
        .order_by(
            FactEvent.id.asc(),
            FactAssertion.field_name.asc(),
            FactAssertion.id.asc(),
        )
    ).all()
    project = db.get(Project, project_id)
    return [
        {
            "event_id": event.id,
            "subject_name": event.subject_name,
            "field": assertion.field_name,
            "value": assertion.value_text,
            "source": assertion.source_name,
            "source_type": assertion.source_type,
            "url": assertion.source_url,
            "evidence": assertion.evidence,
            "resolution_method": assertion.resolution_method,
        }
        for event, assertion in rows
        if not main_report_only
        or (project and not fact_event_exclusion_reason(project, event))
    ]

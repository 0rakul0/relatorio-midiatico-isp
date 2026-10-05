from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.facts.normalization import normalize_fact_value, normalize_person_name, parse_iso_date
from app.facts.operations import sync_operation_events
from app.facts.reporting import fact_event_exclusion_reason
from app.models import FactAssertion, FactEvent, Project


RESOLVABLE_FIELDS = [
    "subject_name", "operation_name", "institution", "rank_or_role", "unit",
    "professional_status", "event_date", "death_date", "cause_category",
    "cause_description", "circumstance", "address", "neighborhood", "city",
    "state", "death_place_name", "death_address", "death_neighborhood",
    "death_city", "death_state",
]

TIME_VARYING_COUNT_FIELDS = [
    "death_count", "arrest_count", "weapon_count", "rifle_count",
]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def resolve_assertions(field_name: str, assertions: list[FactAssertion]) -> tuple[str | None, str, list[str]]:
    usable = [
        assertion
        for assertion in assertions
        if assertion.value_text and assertion.evidence_status == "SUPPORTED"
    ]
    if not usable:
        return None, "NOT_FOUND_IN_SAMPLE", []

    grouped: dict[str, list[FactAssertion]] = {}
    for assertion in usable:
        key = normalize_fact_value(field_name, assertion.value_text)
        if key:
            grouped.setdefault(key, []).append(assertion)

    if not grouped:
        return None, "NOT_FOUND_IN_SAMPLE", []
    if len(grouped) > 1:
        return None, "SOURCE_CONFLICT", [
            values[0].value_text for values in grouped.values()
        ]

    selected = next(iter(grouped.values()))
    value = selected[0].value_text
    has_official = any(a.source_type == "OFFICIAL" for a in selected)
    hosts = {_host(a.source_url) for a in selected if a.source_url}
    status = "CONFIRMED" if has_official or len(hosts) >= 2 else "PARTIALLY_CONFIRMED"
    return value, status, []


def _scope_from_profile(project: Project, event: FactEvent) -> bool | None:
    rules = (project.topic_profile or {}).get("inclusion_rules") or {}
    allowed = rules.get("professional_status")
    if not allowed or not event.professional_status:
        return None
    normalized_allowed = {
        normalize_fact_value("professional_status", str(value))
        for value in allowed
    }
    return normalize_fact_value(
        "professional_status", event.professional_status
    ) in normalized_allowed


def _count_history(assertions: list[FactAssertion], field_name: str) -> list[dict]:
    rows = [a for a in assertions if a.field_name == field_name and a.value_text]
    rows.sort(key=lambda a: (a.reported_at or date.min, a.id or 0))
    return [
        {
            "value": a.value_text,
            "reported_at": a.reported_at.isoformat() if a.reported_at else None,
            "source_name": a.source_name,
            "source_type": a.source_type,
            "source_url": a.source_url,
            "evidence": a.evidence,
        }
        for a in rows
    ]


def _store_time_varying_counts(event: FactEvent, assertions: list[FactAssertion]) -> None:
    extra = dict(event.extra_attributes or {})
    timelines = dict(extra.get("count_timelines") or {})
    for field_name in TIME_VARYING_COUNT_FIELDS:
        history = _count_history(assertions, field_name)
        if not history:
            continue
        timelines[field_name] = history
        latest_official = next(
            (row for row in reversed(history) if row.get("source_type") == "OFFICIAL"),
            None,
        )
        extra[f"{field_name}_latest_official"] = latest_official
        extra[f"{field_name}_latest_reported"] = history[-1]
    if timelines:
        extra["count_timelines"] = timelines
        extra["counts_are_time_varying"] = True
    event.extra_attributes = extra


def resolve_event(db: Session, project: Project, event: FactEvent) -> None:
    assertions = list(
        db.scalars(select(FactAssertion).where(FactAssertion.event_id == event.id)).all()
    )
    conflict_fields: list[str] = []
    field_statuses: list[str] = []

    for field_name in RESOLVABLE_FIELDS:
        field_assertions = [a for a in assertions if a.field_name == field_name]
        value, status, _ = resolve_assertions(field_name, field_assertions)
        field_statuses.append(status)
        if status == "SOURCE_CONFLICT":
            conflict_fields.append(field_name)
            setattr(event, field_name, None)
            if field_name == "subject_name":
                event.normalized_subject_name = None
            continue
        if value is None:
            continue
        if field_name in {"event_date", "death_date"}:
            parsed = parse_iso_date(value)
            if parsed:
                setattr(event, field_name, parsed)
        else:
            setattr(event, field_name, value)
        if field_name == "subject_name":
            event.normalized_subject_name = normalize_person_name(value)

    _store_time_varying_counts(event, assertions)
    event.conflict_fields = sorted(set(conflict_fields))
    event.primary_scope = _scope_from_profile(project, event)
    if conflict_fields:
        event.resolution_status = "SOURCE_CONFLICT"
    elif any(status == "CONFIRMED" for status in field_statuses):
        event.resolution_status = "CONFIRMED"
    elif any(status == "PARTIALLY_CONFIRMED" for status in field_statuses):
        event.resolution_status = "PARTIALLY_CONFIRMED"
    else:
        event.resolution_status = "NOT_FOUND_IN_SAMPLE"

    extra = dict(event.extra_attributes or {})
    reason = fact_event_exclusion_reason(project, event)
    if reason:
        extra["report_exclusion_reason"] = reason
    else:
        extra.pop("report_exclusion_reason", None)
    event.extra_attributes = extra


def resolve_project_facts(db: Session, project: Project) -> dict:
    events = list(
        db.scalars(select(FactEvent).where(FactEvent.project_id == project.id)).all()
    )
    for event in events:
        resolve_event(db, project, event)
    operation_sync = sync_operation_events(db, project)
    db.commit()
    return {
        "events": len(events),
        "confirmed": sum(event.resolution_status == "CONFIRMED" for event in events),
        "partial": sum(event.resolution_status == "PARTIALLY_CONFIRMED" for event in events),
        "conflicts": sum(event.resolution_status == "SOURCE_CONFLICT" for event in events),
        "operation_inventory": operation_sync,
    }

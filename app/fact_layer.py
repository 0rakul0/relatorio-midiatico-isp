from __future__ import annotations

from collections.abc import Callable
from datetime import date
from urllib.parse import urlparse

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm import llm_is_configured, structured_response
from app.models import FactAssertion, FactEvent, MediaItem, Project, SearchQuery
from app.prompts import FACT_EXTRACTION_PROMPT
from app.source_registry import OFFICIAL_SECURITY_SOURCES
from app.topic_profile import normalized_text


FACT_PURPOSES = {"FACT_DISCOVERY", "OFFICIAL_FACT", "NOMINAL_FOLLOWUP"}
RESOLVABLE_FIELDS = [
    "subject_name",
    "institution",
    "rank_or_role",
    "unit",
    "professional_status",
    "event_date",
    "death_date",
    "cause_category",
    "cause_description",
    "circumstance",
    "address",
    "neighborhood",
    "city",
    "state",
    "death_place_name",
    "death_address",
    "death_neighborhood",
    "death_city",
    "death_state",
]


def normalize_person_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(normalized_text(value).split())
    return normalized or None


def normalize_fact_value(field_name: str, value: str | None) -> str:
    if not value:
        return ""
    text = " ".join(normalized_text(value).split())
    if field_name in {"event_date", "death_date"}:
        parsed = _parse_iso_date(value)
        return parsed.isoformat() if parsed else text
    return text


def _parse_iso_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def source_type_for_item(item: MediaItem) -> str:
    host = _host(item.url)
    for source in OFFICIAL_SECURITY_SOURCES:
        domain = source["domain"].lower().split("/")[0]
        if host == domain or host.endswith("." + domain):
            return "OFFICIAL"
    if "youtube.com" in host or "youtu.be" in host:
        return "SOCIAL"
    return "MEDIA"


def _field_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": ["string", "null"]},
            "evidence": {"type": ["string", "null"]},
            "basis": {
                "type": "string",
                "enum": ["EXPLICIT", "RELATIVE_TO_PUBLICATION", "NOT_PRESENT"],
            },
        },
        "required": ["value", "evidence", "basis"],
    }


def _fact_extraction_schema() -> dict:
    field = _field_schema()
    event_properties = {name: field for name in RESOLVABLE_FIELDS}
    event_properties.update(
        {
            "related_to_topic": {"type": "boolean"},
            "event_type": {"type": "string"},
            "subject_type": {"type": ["string", "null"]},
            "relation_reason": {"type": "string"},
        }
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "events": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": event_properties,
                    "required": [
                        "related_to_topic",
                        "event_type",
                        "subject_type",
                        "relation_reason",
                        *RESOLVABLE_FIELDS,
                    ],
                },
            }
        },
        "required": ["events"],
    }


def extract_fact_events_from_item(project: Project, item: MediaItem) -> list[dict]:
    if not llm_is_configured():
        raise RuntimeError("Uma chave de LLM é necessária para a extração factual estruturada")

    settings = get_settings()
    payload = {
        "topic": project.topic,
        "topic_profile": project.topic_profile or {},
        "event_window": {
            "start": project.event_start.isoformat() if project.event_start else None,
            "end": project.event_end.isoformat() if project.event_end else None,
        },
        "source": {
            "title": item.title,
            "url": item.url,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "snippet": item.snippet,
            "content": (item.content or "")[: settings.max_fact_source_chars],
        },
    }
    result = structured_response(
        instructions=FACT_EXTRACTION_PROMPT
        + " Datas resolvidas devem ser retornadas em AAAA-MM-DD. "
        + "related_to_topic só pode ser true quando o próprio texto sustenta relação material com o perfil do tema.",
        payload=payload,
        schema_name="fact_events_from_source",
        schema=_fact_extraction_schema(),
        max_output_tokens=7000,
    )
    return [event for event in result.get("events", []) if event.get("related_to_topic")]


def _event_window_contains(project: Project, event: dict) -> bool | None:
    candidate = _parse_iso_date((event.get("event_date") or {}).get("value"))
    if not candidate:
        candidate = _parse_iso_date((event.get("death_date") or {}).get("value"))
    if not candidate or not project.event_start or not project.event_end:
        return None
    return project.event_start <= candidate <= project.event_end


def _find_event_by_name(db: Session, project: Project, name: str | None) -> FactEvent | None:
    normalized = normalize_person_name(name)
    if not normalized:
        return None
    return db.scalar(
        select(FactEvent).where(
            FactEvent.project_id == project.id,
            FactEvent.normalized_subject_name == normalized,
        )
    )


def _find_conservative_unnamed_event(db: Session, project: Project, event: dict) -> FactEvent | None:
    event_date = _parse_iso_date((event.get("event_date") or {}).get("value"))
    institution = normalize_fact_value("institution", (event.get("institution") or {}).get("value"))
    city = normalize_fact_value("city", (event.get("city") or {}).get("value"))
    if not event_date or not institution or not city:
        return None

    candidates = db.scalars(
        select(FactEvent).where(
            FactEvent.project_id == project.id,
            FactEvent.subject_name.is_(None),
            FactEvent.event_date == event_date,
        )
    ).all()
    matching = [
        candidate
        for candidate in candidates
        if normalize_fact_value("institution", candidate.institution) == institution
        and normalize_fact_value("city", candidate.city) == city
    ]
    return matching[0] if len(matching) == 1 else None


def _get_or_create_event(db: Session, project: Project, extracted: dict) -> FactEvent:
    name = (extracted.get("subject_name") or {}).get("value")
    event = _find_event_by_name(db, project, name)
    if not event:
        event = _find_conservative_unnamed_event(db, project, extracted)
    if event:
        return event

    provisional_event_date = _parse_iso_date((extracted.get("event_date") or {}).get("value"))
    provisional_institution = (extracted.get("institution") or {}).get("value")
    provisional_city = (extracted.get("city") or {}).get("value")
    event = FactEvent(
        project_id=project.id,
        event_type=(extracted.get("event_type") or project.topic_profile.get("event_type") or "OTHER")[:100],
        subject_name=name,
        normalized_subject_name=normalize_person_name(name),
        subject_type=extracted.get("subject_type"),
        event_date=provisional_event_date,
        institution=provisional_institution,
        city=provisional_city,
        resolution_status="PARTIALLY_CONFIRMED",
    )
    db.add(event)
    db.flush()
    return event


def _assertion_exists(
    db: Session,
    event_id: int,
    item_id: int | None,
    field_name: str,
    value: str,
    evidence: str,
) -> bool:
    return bool(
        db.scalar(
            select(FactAssertion.id).where(
                FactAssertion.event_id == event_id,
                FactAssertion.media_item_id == item_id,
                FactAssertion.field_name == field_name,
                FactAssertion.value_text == value,
                FactAssertion.evidence == evidence,
            )
        )
    )


def persist_extracted_event(
    db: Session,
    project: Project,
    item: MediaItem,
    extracted: dict,
) -> FactEvent:
    event = _get_or_create_event(db, project, extracted)
    source_type = source_type_for_item(item)

    for field_name in RESOLVABLE_FIELDS:
        fact = extracted.get(field_name) or {}
        value = fact.get("value")
        evidence = fact.get("evidence")
        basis = fact.get("basis")
        if value in (None, "") or not evidence or basis == "NOT_PRESENT":
            continue
        value = str(value).strip()
        evidence = str(evidence).strip()
        if not value or not evidence:
            continue
        if _assertion_exists(db, event.id, item.id, field_name, value, evidence):
            continue
        db.add(
            FactAssertion(
                event_id=event.id,
                media_item_id=item.id,
                field_name=field_name,
                value_text=value,
                source_url=item.url,
                source_type=source_type,
                source_name=item.source_name or item.domain,
                evidence=evidence[:4000],
                evidence_status="SUPPORTED",
                resolution_method=basis,
            )
        )

    in_window = _event_window_contains(project, extracted)
    if in_window is True:
        item.fact_status = "VALID"
        item.fact_discard_reason = None
    elif in_window is False:
        item.fact_status = "OUTSIDE_EVENT_WINDOW"
        item.fact_discard_reason = "O fato extraído ocorreu fora da janela factual solicitada"
    else:
        item.fact_status = "DATE_UNVERIFIED"
        item.fact_discard_reason = "A fonte é relevante, mas não permite confirmar a data do fato"

    return event


def _query_purpose_map(db: Session, project_id: int) -> dict[int, str]:
    rows = db.execute(select(SearchQuery.id, SearchQuery.purpose).where(SearchQuery.project_id == project_id)).all()
    return {query_id: purpose for query_id, purpose in rows}


def _item_should_feed_fact_layer(item: MediaItem, purpose_by_query: dict[int, str], project: Project) -> bool:
    purposes = set(item.discovery_purposes or [])
    if item.query_id and purpose_by_query.get(item.query_id):
        purposes.add(purpose_by_query[item.query_id])
    if purposes.intersection(FACT_PURPOSES):
        return True
    # Em pauta factual, item manual também pode ser usado para prova factual.
    return project.project_type == "EVENT_TOPIC" and item.search_source == "manual"


def extract_project_facts(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    if project.project_type != "EVENT_TOPIC":
        return {"processed": 0, "events_extracted": 0, "errors": 0}

    purpose_by_query = _query_purpose_map(db, project.id)
    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    processed = events_extracted = errors = 0

    eligible_items = [item for item in items if _item_should_feed_fact_layer(item, purpose_by_query, project)]
    total_eligible = len(eligible_items)

    for item_index, item in enumerate(eligible_items, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Fonte factual {item_index}/{total_eligible}: {item.title[:90]}")
        if not _item_should_feed_fact_layer(item, purpose_by_query, project):
            continue
        # VALID pode ser reprocessado depois de uma nova fonte, mas o mesmo item
        # não precisa chamar a LLM repetidamente se já gerou assertions.
        already_has_assertions = db.scalar(
            select(FactAssertion.id).where(FactAssertion.media_item_id == item.id).limit(1)
        )
        if already_has_assertions:
            continue

        processed += 1
        try:
            extracted_events = extract_fact_events_from_item(project, item)
        except RuntimeError as exc:
            item.fact_status = "EXTRACTION_FAILED"
            item.fact_discard_reason = str(exc)[:1000]
            errors += 1
            continue

        if not extracted_events:
            item.fact_status = "NOT_RELATED"
            item.fact_discard_reason = "Nenhum evento relacionado ao tema foi sustentado pelo texto"
            continue

        for extracted in extracted_events:
            persist_extracted_event(db, project, item, extracted)
            events_extracted += 1

    db.commit()
    return {"processed": processed, "events_extracted": events_extracted, "errors": errors}


def _resolve_assertions(field_name: str, assertions: list[FactAssertion]) -> tuple[str | None, str, list[str]]:
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
        distinct_values = [values[0].value_text for values in grouped.values()]
        return None, "SOURCE_CONFLICT", distinct_values

    selected = next(iter(grouped.values()))
    value = selected[0].value_text
    has_official = any(assertion.source_type == "OFFICIAL" for assertion in selected)
    independent_hosts = {_host(assertion.source_url) for assertion in selected if assertion.source_url}
    status = "CONFIRMED" if has_official or len(independent_hosts) >= 2 else "PARTIALLY_CONFIRMED"
    return value, status, []


def _scope_from_profile(project: Project, event: FactEvent) -> bool | None:
    rules = (project.topic_profile or {}).get("inclusion_rules") or {}
    allowed_statuses = rules.get("professional_status")
    if not allowed_statuses or not event.professional_status:
        return None
    normalized_allowed = {normalize_fact_value("professional_status", str(value)) for value in allowed_statuses}
    return normalize_fact_value("professional_status", event.professional_status) in normalized_allowed


def fact_event_exclusion_reason(project: Project, event: FactEvent) -> str | None:
    """Return the reason an event must stay out of the report's factual core.

    The source assertions remain stored for audit, but a source that merely
    mentions a related event cannot introduce it into a report for another
    requested period.
    """
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


def _set_scope_audit_marker(project: Project, event: FactEvent) -> None:
    """Keep the exclusion decision attached to the event without deleting evidence."""
    extra = dict(event.extra_attributes or {})
    reason = fact_event_exclusion_reason(project, event)
    if reason:
        extra["report_exclusion_reason"] = reason
    else:
        extra.pop("report_exclusion_reason", None)
    event.extra_attributes = extra


def resolve_event(db: Session, project: Project, event: FactEvent) -> None:
    assertions = db.scalars(select(FactAssertion).where(FactAssertion.event_id == event.id)).all()
    conflict_fields: list[str] = []
    field_statuses: list[str] = []

    for field_name in RESOLVABLE_FIELDS:
        field_assertions = [a for a in assertions if a.field_name == field_name]
        value, status, conflicts = _resolve_assertions(field_name, field_assertions)
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
            parsed = _parse_iso_date(value)
            if parsed:
                setattr(event, field_name, parsed)
        else:
            setattr(event, field_name, value)

        if field_name == "subject_name":
            event.normalized_subject_name = normalize_person_name(value)

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
    _set_scope_audit_marker(project, event)


def resolve_project_facts(db: Session, project: Project) -> dict:
    events = db.scalars(select(FactEvent).where(FactEvent.project_id == project.id)).all()
    for event in events:
        resolve_event(db, project, event)
    db.commit()
    return {
        "events": len(events),
        "confirmed": sum(event.resolution_status == "CONFIRMED" for event in events),
        "partial": sum(event.resolution_status == "PARTIALLY_CONFIRMED" for event in events),
        "conflicts": sum(event.resolution_status == "SOURCE_CONFLICT" for event in events),
    }


def plan_nominal_followups(db: Session, project: Project) -> int:
    events = db.scalars(
        select(FactEvent).where(
            FactEvent.project_id == project.id,
            FactEvent.subject_name.is_not(None),
        )
    ).all()
    existing = set(db.scalars(select(SearchQuery.query).where(SearchQuery.project_id == project.id)).all())
    created = 0

    for event in events:
        name = (event.subject_name or "").strip()
        if not name:
            continue
        location = event.city or ((project.topic_profile or {}).get("locations") or ["Rio de Janeiro"])[0]
        organization = event.institution or ""
        queries = [
            f'"{name}"',
            f'"{name}" {organization}'.strip(),
            f'"{name}" "{location}"',
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


def fact_events_for_report(db: Session, project_id: int) -> list[dict]:
    project = db.get(Project, project_id)
    events = db.scalars(
        select(FactEvent)
        .where(FactEvent.project_id == project_id)
        .order_by(FactEvent.event_date.asc().nullslast(), FactEvent.subject_name.asc().nullslast(), FactEvent.id.asc())
    ).all()
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
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
            "report_exclusion_reason": fact_event_exclusion_reason(project, event) if project else None,
        }
        for event in events
    ]


def fact_events_for_main_report(db: Session, project_id: int) -> list[dict]:
    """Return only resolved events that belong to the requested factual window.

    ``fact_events_for_report`` deliberately remains the complete, auditable
    inventory used by the factual-review screen. This narrower projection is
    the sole input for metrics, drafting, PDF generation and QA.
    """
    project = db.get(Project, project_id)
    if not project:
        return []
    return [event for event in fact_events_for_report(db, project_id) if not event["report_exclusion_reason"]]


def fact_event_validation_summary(db: Session, project_id: int) -> dict:
    """Summarize included and retained-but-excluded factual events."""
    project = db.get(Project, project_id)
    if not project:
        return {"total": 0, "eligible": 0, "excluded": 0, "excluded_by_reason": {}}
    events = db.scalars(select(FactEvent).where(FactEvent.project_id == project_id)).all()
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
        .order_by(FactEvent.id.asc(), FactAssertion.field_name.asc(), FactAssertion.id.asc())
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
        if not main_report_only or (project and not fact_event_exclusion_reason(project, event))
    ]

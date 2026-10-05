from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.agent import get_report_agent
from app.llm import llm_is_configured
from app.schemas import FactExtractionResponse
from app.models import FactAssertion, FactEvent, MediaItem, Project
from app.source_registry import OFFICIAL_SECURITY_SOURCES
from app.facts.normalization import (
    event_identity_key,
    extracted_value as _extracted_value,
    identity_conflicts as _identity_conflicts,
    normalize_fact_value,
    normalize_person_name,
    parse_iso_date as _parse_iso_date,
    same_known_date as _same_known_date,
)
from app.facts.operations import (
    operation_display_name as _operation_display_name,
    operation_events_for_report,
    operation_inventory_summary,
    operation_mentions_for_report,
)
from app.facts.reporting import (
    fact_assertions_for_report,
    fact_event_exclusion_reason,
    fact_event_validation_summary,
    fact_events_for_main_report,
    fact_events_for_report,
)
from app.facts.resolution import (
    resolve_assertions,
    resolve_event,
    resolve_project_facts,
)
from app.facts.followups import plan_nominal_followups
from app.facts.selection import (
    item_should_feed_fact_layer as _item_should_feed_fact_layer,
    is_annual_police_operation_project as _is_annual_police_operation_project,
    query_purpose_map as _query_purpose_map,
)


RESOLVABLE_FIELDS = [
    "subject_name",
    "operation_name",
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

TIME_VARYING_COUNT_FIELDS = [
    "death_count",
    "arrest_count",
    "weapon_count",
    "rifle_count",
]


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


FACT_EXTRACTION_SCHEMA = FactExtractionResponse


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
    result = get_report_agent().run(
        task="fact_extraction",
        extra_instructions=(
            "Datas resolvidas devem ser retornadas em AAAA-MM-DD. "
            "related_to_topic só pode ser true quando o próprio texto sustenta "
            "relação material com o perfil do tema."
        ),
        payload=payload,
        schema_name="fact_events_from_source",
        response_model=FACT_EXTRACTION_SCHEMA,
        max_output_tokens=7000,
    )
    events = [event for event in result.get("events", []) if event.get("related_to_topic")]
    return _reject_unanchored_relative_dates(events, item)


def _reject_unanchored_relative_dates(events: list[dict], item: MediaItem) -> list[dict]:
    """Descarta datas relativas quando não há publicação para ancorá-las."""
    if item.published_at:
        return events
    for event in events:
        for field_name in ("event_date", "death_date"):
            fact = event.get(field_name)
            if isinstance(fact, dict) and fact.get("basis") == "RELATIVE_TO_PUBLICATION":
                event[field_name] = {"value": None, "evidence": None, "basis": "NOT_PRESENT"}
    return events


def _fact_extraction_priority(item: MediaItem) -> tuple:
    """Ordena a extração: oficial, conteúdo completo, data confiável, demais."""
    source_rank = 0 if source_type_for_item(item) == "OFFICIAL" else 1
    content_rank = 0 if len(item.content or "") >= 400 else 1
    date_rank = 0 if item.published_at else 1
    return (source_rank, content_rank, date_rank, item.id or 0)


def _event_window_contains(project: Project, event: dict) -> bool | None:
    candidate = _parse_iso_date((event.get("event_date") or {}).get("value"))
    if not candidate:
        candidate = _parse_iso_date((event.get("death_date") or {}).get("value"))
    if not candidate or not project.event_start or not project.event_end:
        return None
    return project.event_start <= candidate <= project.event_end


def _extracted_value(extracted: dict, field_name: str) -> str | None:
    return (extracted.get(field_name) or {}).get("value")


def _identity_conflicts(event: FactEvent, extracted: dict) -> bool:
    """Indica se algum atributo presente nos dois lados diverge.

    Nome igual não basta: duas pessoas diferentes podem compartilhar o mesmo
    nome. Só tratamos como o mesmo fato quando data, instituição, unidade,
    local e cargo são compatíveis (ou ausentes de um dos lados).
    """
    for field_name in ("institution", "unit", "city", "state", "rank_or_role"):
        extracted_value = normalize_fact_value(field_name, _extracted_value(extracted, field_name))
        stored_value = normalize_fact_value(field_name, getattr(event, field_name, None))
        if extracted_value and stored_value and extracted_value != stored_value:
            return True
    for field_name in ("event_date", "death_date"):
        extracted_date = _parse_iso_date(_extracted_value(extracted, field_name))
        stored_date = getattr(event, field_name, None)
        if extracted_date and stored_date and extracted_date != stored_date:
            return True
    return False


def _same_known_date(event: FactEvent, extracted: dict) -> bool:
    for field_name in ("event_date", "death_date"):
        extracted_date = _parse_iso_date(_extracted_value(extracted, field_name))
        if extracted_date and extracted_date == getattr(event, field_name, None):
            return True
    return False


def _find_event_by_name(db: Session, project: Project, name: str | None, extracted: dict) -> FactEvent | None:
    normalized = normalize_person_name(name)
    if not normalized:
        return None
    candidates = db.scalars(
        select(FactEvent)
        .where(
            FactEvent.project_id == project.id,
            FactEvent.normalized_subject_name == normalized,
        )
        .order_by(FactEvent.id.asc())
    ).all()
    if not candidates:
        return None

    compatible = [candidate for candidate in candidates if not _identity_conflicts(candidate, extracted)]
    if len(compatible) == 1:
        return compatible[0]
    if len(compatible) > 1:
        # Tenta desambiguar pela data do fato. Quando ainda há ambiguidade,
        # NÃO funde automaticamente: cria um novo evento e sinaliza possível
        # duplicidade para revisão humana.
        same_date = [candidate for candidate in compatible if _same_known_date(candidate, extracted)]
        if len(same_date) == 1:
            return same_date[0]
        for candidate in compatible:
            _mark_possible_duplicate(candidate, normalize_person_name(name))
        return None
    # Há um homônimo, mas os atributos conflitam: não fundir; cria novo evento.
    return None


def _mark_possible_duplicate(event: FactEvent, name: str | None) -> None:
    extra = dict(event.extra_attributes or {})
    duplicates = set(extra.get("possible_duplicate_subjects") or [])
    if name:
        duplicates.add(name)
    extra["possible_duplicate_subjects"] = sorted(duplicates)
    extra["possible_duplicate"] = True
    event.extra_attributes = extra


def _find_conservative_unnamed_event(db: Session, project: Project, event: dict) -> FactEvent | None:
    event_date = _parse_iso_date((event.get("event_date") or {}).get("value"))
    operation_name = normalize_fact_value(
        "operation_name", (event.get("operation_name") or {}).get("value")
    )
    institution = normalize_fact_value("institution", (event.get("institution") or {}).get("value"))
    city = normalize_fact_value("city", (event.get("city") or {}).get("value"))
    if not event_date or not city:
        return None

    candidates = db.scalars(
        select(FactEvent).where(
            FactEvent.project_id == project.id,
            FactEvent.subject_name.is_(None),
            FactEvent.event_date == event_date,
        )
    ).all()
    matching: list[FactEvent] = []
    for candidate in candidates:
        candidate_operation = normalize_fact_value("operation_name", candidate.operation_name)
        if operation_name:
            # Nome da operação + data + cidade é identidade muito mais segura
            # que instituição + cidade para dias com várias ações policiais.
            if candidate_operation != operation_name:
                continue
        elif candidate_operation:
            # Não funde uma ocorrência genérica em uma operação já nomeada.
            continue
        if normalize_fact_value("city", candidate.city) != city:
            continue
        if institution and candidate.institution:
            if normalize_fact_value("institution", candidate.institution) != institution:
                continue
        matching.append(candidate)
    return matching[0] if len(matching) == 1 else None


def _get_or_create_event(db: Session, project: Project, extracted: dict) -> FactEvent:
    name = (extracted.get("subject_name") or {}).get("value")
    event = _find_event_by_name(db, project, name, extracted)
    if not event:
        event = _find_conservative_unnamed_event(db, project, extracted)
    if event:
        return event

    provisional_event_date = _parse_iso_date((extracted.get("event_date") or {}).get("value"))
    provisional_operation_name = (extracted.get("operation_name") or {}).get("value")
    provisional_institution = (extracted.get("institution") or {}).get("value")
    provisional_city = (extracted.get("city") or {}).get("value")
    event = FactEvent(
        project_id=project.id,
        event_type=(extracted.get("event_type") or (project.topic_profile or {}).get("event_type") or "OTHER")[:100],
        operation_name=provisional_operation_name,
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
    identity_key = event_identity_key(extracted)
    if identity_key:
        extra = dict(event.extra_attributes or {})
        extra["event_identity_key"] = identity_key
        event.extra_attributes = extra
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

    for field_name in [*RESOLVABLE_FIELDS, *TIME_VARYING_COUNT_FIELDS]:
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
                reported_at=item.published_at,
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
    settings = get_settings()
    extraction_cap = max(1, settings.max_fact_extractions)
    if _is_annual_police_operation_project(project):
        # Inventário anual prioriza recall: o teto factual padrão (20) era
        # insuficiente para cobrir todas as operações/matérias ao longo de 12 meses.
        extraction_cap = max(extraction_cap, 60)
    processed = events_extracted = errors = 0

    eligible_items = [item for item in items if _item_should_feed_fact_layer(item, purpose_by_query, project)]
    eligible_items.sort(key=_fact_extraction_priority)
    total_eligible = len(eligible_items)

    for item_index, item in enumerate(eligible_items, start=1):
        if processed >= extraction_cap:
            break
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

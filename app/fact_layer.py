from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from urllib.parse import urlparse

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.agent import get_report_agent
from app.llm import llm_is_configured
from app.schemas import FactExtractionResponse
from app.models import FactAssertion, FactEvent, MediaItem, Project, SearchQuery
from app.source_registry import OFFICIAL_SECURITY_SOURCES
from app.topic_profile import normalized_text


FACT_PURPOSES = {"FACT_DISCOVERY", "OFFICIAL_FACT", "NOMINAL_FOLLOWUP"}
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


def normalize_person_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(normalized_text(value).split())
    return normalized or None


_RANK_ABBREVIATIONS = {
    "cel": "coronel",
    "ten": "tenente",
    "cap": "capitao",
    "maj": "major",
    "sgt": "sargento",
    "cb": "cabo",
    "sd": "soldado",
    "insp": "inspetor",
    "del": "delegado",
    "subten": "subtenente",
}

_INSTITUTION_ALIASES = {
    "pmrj": "policia militar do estado do rio de janeiro",
    "pm erj": "policia militar do estado do rio de janeiro",
    "pcerj": "policia civil do estado do rio de janeiro",
    "pc erj": "policia civil do estado do rio de janeiro",
    "isp": "instituto de seguranca publica",
    "isp rj": "instituto de seguranca publica",
}

_PROFESSIONAL_STATUS_ALIASES = {
    "da ativa": "ativo",
    "em atividade": "ativo",
    "na ativa": "ativo",
    "reformado": "reforma",
    "aposentado": "aposentadoria",
}


def _normalize_unit(value: str) -> str:
    text = normalized_text(value).replace("º", "").replace("°", "")
    text = text.replace(" n. ", " ").replace("no.", "")
    text = " ".join(text.replace(".", " ").split())
    text = re.sub(r"\b0+(\d)", r"\1", text)
    return text


def _normalize_rank(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", normalized_text(value))
    expanded = [_RANK_ABBREVIATIONS.get(token, token) for token in tokens]
    return " ".join(expanded)


def _normalize_institution(value: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9]+", normalized_text(value)))
    if not text:
        return ""
    for alias, canonical in _INSTITUTION_ALIASES.items():
        if text == alias or text.replace(" ", "") == alias.replace(" ", ""):
            return canonical
    return text


def _normalize_professional_status(value: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9]+", normalized_text(value)))
    return _PROFESSIONAL_STATUS_ALIASES.get(text, text)


def normalize_fact_value(field_name: str, value: str | None) -> str:
    if not value:
        return ""
    if field_name in {"event_date", "death_date"}:
        parsed = _parse_iso_date(value)
        return parsed.isoformat() if parsed else " ".join(normalized_text(value).split())
    if field_name == "unit":
        return _normalize_unit(value)
    if field_name == "rank_or_role":
        return _normalize_rank(value)
    if field_name == "institution":
        return _normalize_institution(value)
    if field_name == "professional_status":
        return _normalize_professional_status(value)
    return " ".join(normalized_text(value).split())


def event_identity_key(extracted: dict) -> str | None:
    """Chave conservadora para decidir se dois eventos são o mesmo fato.

    Só devolve uma chave quando há informação suficiente para uma fusão
    segura: nome normalizado ou (data + instituição + cidade). Caso contrário,
    retorna None e o chamador deve manter os eventos separados.
    """
    name = normalize_person_name((extracted.get("subject_name") or {}).get("value"))
    operation_name = normalize_fact_value("operation_name", _extracted_value(extracted, "operation_name"))
    event_date = _parse_iso_date((extracted.get("event_date") or {}).get("value"))
    death_date = _parse_iso_date((extracted.get("death_date") or {}).get("value"))
    institution = normalize_fact_value("institution", _extracted_value(extracted, "institution"))
    unit = normalize_fact_value("unit", _extracted_value(extracted, "unit"))
    city = normalize_fact_value("city", _extracted_value(extracted, "city"))

    parts: list[str] = []
    if name:
        parts.append(f"name={name}")
    if operation_name:
        parts.append(f"operation={operation_name}")
    event_date_iso = (event_date or death_date)
    if event_date_iso:
        parts.append(f"date={event_date_iso.isoformat()}")
    if institution:
        parts.append(f"institution={institution}")
    if unit:
        parts.append(f"unit={unit}")
    if city:
        parts.append(f"city={city}")

    if name and (event_date_iso or institution or unit):
        return "|".join(parts)
    if operation_name and event_date_iso and city:
        return "|".join(parts)
    if event_date_iso and institution and city and not operation_name:
        return "|".join(parts)
    return None


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
    settings = get_settings()
    extraction_cap = max(1, settings.max_fact_extractions)
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
    """Preserva evolução de balanços sem convertê-la em conflito estrutural.

    Uma operação em andamento pode ter 60, 64, 119, 121... mortos conforme o
    balanço é atualizado. Esses valores são uma série temporal atribuída às
    fontes, não valores simultâneos obrigatoriamente incompatíveis.
    """
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
    assertions = db.scalars(select(FactAssertion).where(FactAssertion.event_id == event.id)).all()
    conflict_fields: list[str] = []
    field_statuses: list[str] = []

    for field_name in RESOLVABLE_FIELDS:
        field_assertions = [a for a in assertions if a.field_name == field_name]
        value, status, conflicts = resolve_assertions(field_name, field_assertions)
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

    _store_time_varying_counts(event, assertions)
    # Campos de contagem são deliberadamente excluídos de conflict_fields:
    # divergência temporal é representada em count_timelines. Conflito factual
    # continua valendo para campos estáveis (data, local, identidade etc.).
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
            "counts_are_time_varying": bool((event.extra_attributes or {}).get("counts_are_time_varying")),
            "death_count_latest_official": (event.extra_attributes or {}).get("death_count_latest_official"),
            "death_count_latest_reported": (event.extra_attributes or {}).get("death_count_latest_reported"),
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

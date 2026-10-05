from __future__ import annotations

import re
from datetime import date

from app.topic_profile import normalized_text


RANK_ABBREVIATIONS = {
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

INSTITUTION_ALIASES = {
    "pmrj": "policia militar do estado do rio de janeiro",
    "pm erj": "policia militar do estado do rio de janeiro",
    "pcerj": "policia civil do estado do rio de janeiro",
    "pc erj": "policia civil do estado do rio de janeiro",
    "isp": "instituto de seguranca publica",
    "isp rj": "instituto de seguranca publica",
}

PROFESSIONAL_STATUS_ALIASES = {
    "da ativa": "ativo",
    "em atividade": "ativo",
    "na ativa": "ativo",
    "reformado": "reforma",
    "aposentado": "aposentadoria",
}


def parse_iso_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def extracted_value(extracted: dict, field_name: str) -> str | None:
    return (extracted.get(field_name) or {}).get("value")


def normalize_person_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = " ".join(normalized_text(value).split())
    return normalized or None


def _normalize_unit(value: str) -> str:
    text = normalized_text(value).replace("º", "").replace("°", "")
    text = text.replace(" n. ", " ").replace("no.", "")
    text = " ".join(text.replace(".", " ").split())
    return re.sub(r"\b0+(\d)", r"\1", text)


def _normalize_rank(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", normalized_text(value))
    return " ".join(RANK_ABBREVIATIONS.get(token, token) for token in tokens)


def _normalize_institution(value: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9]+", normalized_text(value)))
    if not text:
        return ""
    for alias, canonical in INSTITUTION_ALIASES.items():
        if text == alias or text.replace(" ", "") == alias.replace(" ", ""):
            return canonical
    return text


def _normalize_professional_status(value: str) -> str:
    text = " ".join(re.findall(r"[a-z0-9]+", normalized_text(value)))
    return PROFESSIONAL_STATUS_ALIASES.get(text, text)


def normalize_fact_value(field_name: str, value: str | None) -> str:
    if not value:
        return ""
    if field_name in {"event_date", "death_date"}:
        parsed = parse_iso_date(value)
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
    name = normalize_person_name((extracted.get("subject_name") or {}).get("value"))
    operation_name = normalize_fact_value("operation_name", extracted_value(extracted, "operation_name"))
    event_date = parse_iso_date((extracted.get("event_date") or {}).get("value"))
    death_date = parse_iso_date((extracted.get("death_date") or {}).get("value"))
    institution = normalize_fact_value("institution", extracted_value(extracted, "institution"))
    unit = normalize_fact_value("unit", extracted_value(extracted, "unit"))
    city = normalize_fact_value("city", extracted_value(extracted, "city"))

    parts: list[str] = []
    if name:
        parts.append(f"name={name}")
    if operation_name:
        parts.append(f"operation={operation_name}")
    event_date_iso = event_date or death_date
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


def identity_conflicts(event: object, extracted: dict) -> bool:
    for field_name in ("institution", "unit", "city", "state", "rank_or_role"):
        extracted_norm = normalize_fact_value(field_name, extracted_value(extracted, field_name))
        stored_norm = normalize_fact_value(field_name, getattr(event, field_name, None))
        if extracted_norm and stored_norm and extracted_norm != stored_norm:
            return True
    for field_name in ("event_date", "death_date"):
        extracted_date = parse_iso_date(extracted_value(extracted, field_name))
        stored_date = getattr(event, field_name, None)
        if extracted_date and stored_date and extracted_date != stored_date:
            return True
    return False


def same_known_date(event: object, extracted: dict) -> bool:
    for field_name in ("event_date", "death_date"):
        extracted_date = parse_iso_date(extracted_value(extracted, field_name))
        if extracted_date and extracted_date == getattr(event, field_name, None):
            return True
    return False

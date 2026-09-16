from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date

from app.llm import llm_is_configured, structured_response
from app.prompts import TOPIC_PROFILE_PROMPT


MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}


def normalized_text(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", (text or "").lower())
        if not unicodedata.combining(char)
    )


def normalized_terms(text: str) -> set[str]:
    return {
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]+", normalized_text(text))
        if len(token) >= 3 and not token.isdigit()
    }


def requested_month_window(topic: str) -> tuple[date, date] | None:
    normalized = normalized_text(topic)
    matches = re.findall(r"\b(" + "|".join(MONTHS_PT) + r")\s+(?:de\s+)?(20\d{2})\b", normalized)
    unique = {(MONTHS_PT[month], int(year)) for month, year in matches}
    if len(unique) != 1:
        return None
    month, year = unique.pop()
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _heuristic_project_type(topic: str) -> str:
    text = normalized_text(topic)
    product_terms = ("dossie", "relatorio", "boletim", "anuario", "estudo", "publicacao")
    event_terms = (
        "morto", "morta", "mortos", "mortas", "morte", "assassinado", "assassinada",
        "operacao", "apreensao", "prisao", "vitima", "ferido", "feminicidio", "homicidio",
    )
    if any(term in text for term in product_terms):
        return "INSTITUTIONAL_PRODUCT"
    if requested_month_window(topic) or any(term in text for term in event_terms):
        return "EVENT_TOPIC"
    return "GENERAL_TOPIC"


def heuristic_topic_profile(topic: str) -> dict:
    text = normalized_text(topic)
    project_type = _heuristic_project_type(topic)
    actors: list[str] = []
    actions: list[str] = []
    organizations: list[str] = []
    requested_fact_fields: list[str] = []
    search_synonyms: list[str] = []
    event_type = "OTHER"

    if "policial" in text or "policia" in text:
        actors = ["policial", "policial militar", "PM", "policial civil", "inspetor", "policial penal", "agente"]
        organizations = ["PMERJ", "Polícia Civil RJ", "SEAP RJ"]
        search_synonyms.extend(actors)
    if any(term in text for term in ("morto", "morta", "mortos", "mortas", "morte", "assassinado", "assassinada")):
        event_type = "DEATH"
        actions = ["morto", "morreu", "assassinado", "falecido", "baleado", "morte"]
        search_synonyms.extend(actions)
        requested_fact_fields = [
            "subject_name", "institution", "rank_or_role", "unit", "professional_status",
            "event_date", "death_date", "cause_description", "circumstance",
            "address", "neighborhood", "city", "state",
            "death_place_name", "death_address", "death_neighborhood", "death_city", "death_state",
        ]

    locations = []
    if "rio de janeiro" in text or re.search(r"\brj\b", text):
        locations = ["Rio de Janeiro", "RJ", "estado do Rio de Janeiro"]

    if not search_synonyms:
        search_synonyms = sorted(normalized_terms(topic))

    return {
        "project_type": project_type,
        "event_type": event_type,
        "actors": actors,
        "actions": actions,
        "locations": locations,
        "organizations": organizations,
        "search_synonyms": list(dict.fromkeys(search_synonyms))[:30],
        "requested_fact_fields": requested_fact_fields,
        "inclusion_rules": {"professional_status": [], "notes": []},
        "exclusion_rules": {"professional_status": [], "notes": []},
    }


def build_topic_profile(topic: str) -> dict:
    fallback = heuristic_topic_profile(topic)
    if not llm_is_configured():
        return fallback

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_type": {
                "type": "string",
                "enum": ["INSTITUTIONAL_PRODUCT", "EVENT_TOPIC", "GENERAL_TOPIC"],
            },
            "event_type": {"type": "string"},
            "actors": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "actions": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "locations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "organizations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "search_synonyms": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "requested_fact_fields": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "inclusion_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "professional_status": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                },
                "required": ["professional_status", "notes"],
            },
            "exclusion_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "professional_status": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                },
                "required": ["professional_status", "notes"],
            },
        },
        "required": [
            "project_type", "event_type", "actors", "actions", "locations", "organizations",
            "search_synonyms", "requested_fact_fields", "inclusion_rules", "exclusion_rules",
        ],
    }
    try:
        result = structured_response(
            instructions=TOPIC_PROFILE_PROMPT,
            payload={"topic": topic},
            schema_name="topic_profile",
            schema=schema,
        )
    except RuntimeError:
        return fallback

    # Evita um perfil LLM pobre eliminando pistas úteis do fallback.
    for key in ("actors", "actions", "locations", "organizations", "search_synonyms", "requested_fact_fields"):
        result[key] = list(dict.fromkeys([*(result.get(key) or []), *(fallback.get(key) or [])]))
    if result.get("project_type") == "GENERAL_TOPIC" and fallback["project_type"] == "EVENT_TOPIC":
        result["project_type"] = "EVENT_TOPIC"
    return result

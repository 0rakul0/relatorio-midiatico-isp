from __future__ import annotations

import re

from app.models import Project
from app.search.guards import (
    is_redundant,
    media_query_is_acceptable,
    query_tokens,
)
from app.topic_profile import normalized_text


STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma",
}

ZERO_RECOVERY_EXPANSIONS: dict[str, list[str]] = {
    "habitacional": ["imoveis", "construcao", "mercado imobiliario", "moradia"],
    "habitacao": ["imoveis", "construcao", "mercado imobiliario", "moradia"],
    "moradia": ["imoveis", "construcao", "mercado imobiliario", "habitacao"],
    "imobiliario": ["imoveis", "construcao", "mercado imobiliario"],
    "imobiliaria": ["imoveis", "construcao", "mercado imobiliario"],
    "milicia": ["milicia", "milicianos"],
    "miliciano": ["milicia", "milicianos"],
}


def has_site_operator(query: str) -> bool:
    return bool(re.search(r"(?:^|\s)site:[^\s]+", query or "", flags=re.IGNORECASE))


def gap_fallback_angles(
    project: Project,
    executed: list[str],
    cap: int,
) -> list[tuple[str, str]]:
    profile = project.topic_profile or {}
    pools: list[str] = []
    for key in (
        "event_search_variants",
        "fact_discovery_variants",
        "product_search_variants",
        "subject_terms",
        "search_synonyms",
        "event_anchor",
        "product_anchor",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            pools.extend(str(item) for item in value if str(item).strip())
        elif value:
            pools.append(str(value))
    pools.append(project.topic)

    seen: set[str] = set()
    phrases: list[str] = []
    for phrase in pools:
        compact = " ".join(str(phrase).split()).strip()
        key = normalized_text(compact)
        if compact and key not in seen:
            seen.add(key)
            phrases.append(compact)

    selected = list(executed)
    output: list[tuple[str, str]] = []
    for phrase in phrases:
        if len(output) >= max(0, cap):
            break
        if not media_query_is_acceptable(project, phrase):
            continue
        if is_redundant(phrase, selected):
            continue
        output.append((phrase, "Ângulo do perfil ainda não executado na web aberta."))
        selected.append(phrase)
    return output


def zero_corpus_fallback_angles(
    project: Project,
    executed: list[str],
    cap: int,
) -> list[tuple[str, str]]:
    if cap <= 0:
        return []

    profile = project.topic_profile or {}
    locations = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("locations") or [])
        if str(value).strip()
    ]
    location = locations[0] if locations else ""
    topic_norm = normalized_text(project.topic or "")
    topic_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", topic_norm)
        if token not in STOPWORDS and len(token) > 2
    ]

    location_tokens: set[str] = set()
    for value in locations:
        location_tokens.update(query_tokens(value))
    core_tokens = [
        token
        for token in topic_tokens
        if token not in location_tokens and token not in {"producao", "perfil", "tema"}
    ]

    expansions: list[str] = []
    for token in topic_tokens:
        expansions.extend(ZERO_RECOVERY_EXPANSIONS.get(token, []))

    anchor = f'"{location}"' if location else ""
    base_core = [
        token
        for token in core_tokens
        if token not in {
            "habitacional",
            "habitacao",
            "moradia",
            "imobiliario",
            "imobiliaria",
        }
    ]
    if not base_core:
        base_core = core_tokens[:2]

    candidates: list[str] = []
    for expansion in list(dict.fromkeys(expansions)):
        parts = [anchor, *base_core[:1], expansion]
        query = " ".join(part for part in parts if part).strip()
        if query:
            candidates.append(query)

    broad_parts = [anchor, *base_core[:2]]
    broad = " ".join(part for part in broad_parts if part).strip()
    if broad:
        candidates.append(broad)

    selected = list(executed)
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = " ".join(normalized_text(candidate).split())
        if not key or key in seen:
            continue
        seen.add(key)
        if not media_query_is_acceptable(project, candidate):
            continue
        if is_redundant(candidate, selected):
            continue
        output.append((
            candidate,
            "Recuperação obrigatória após corpus zero: linguagem jornalística/consulta mais ampla.",
        ))
        selected.append(candidate)
        if len(output) >= cap:
            break
    return output

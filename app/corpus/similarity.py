from __future__ import annotations

import re

from app.models import CorpusDocument, Project
from app.topic_profile import normalized_text
from app.year_utils import find_years


STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma", "ao",
    "aos", "que", "sobre", "rj", "rio", "janeiro",
}


def tokens(value: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized_text(value or ""))
        if len(token) > 2 and token not in STOPWORDS
    }


def compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def profile_terms(project: Project) -> tuple[set[str], list[str]]:
    profile = project.topic_profile or {}
    values = [project.topic]
    phrases: list[str] = []

    for key in ("product_name", "product_anchor", "event_anchor"):
        value = compact(profile.get(key))
        if value:
            values.append(value)
            phrases.append(value)

    for key in (
        "product_search_variants",
        "event_search_variants",
        "fact_discovery_variants",
        "subject_terms",
        "actors",
        "actions",
        "locations",
        "organizations",
        "search_synonyms",
    ):
        values.extend(
            compact(value)
            for value in (profile.get(key) or [])
            if compact(value)
        )

    result: set[str] = set()
    for value in values:
        result.update(tokens(value))
    return result, phrases


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def project_years(project: Project) -> set[str]:
    profile = project.topic_profile or {}
    years: set[str] = set()
    for value in [
        project.topic,
        compact(profile.get("product_name")),
        compact(profile.get("product_anchor")),
        compact(profile.get("event_anchor")),
    ]:
        years.update(find_years(value or ""))

    if project.event_start:
        years.add(str(project.event_start.year))
    if project.event_end:
        years.add(str(project.event_end.year))
    return years


def project_similarity(current: Project, previous: Project) -> float:
    current_topic = normalized_text(current.topic or "")
    previous_topic = normalized_text(previous.topic or "")
    if current_topic and current_topic == previous_topic:
        return 1.0

    current_profile = current.topic_profile or {}
    previous_profile = previous.topic_profile or {}

    current_product = normalized_text(
        compact(
            current_profile.get("product_anchor")
            or current_profile.get("product_name")
        )
    )
    previous_product = normalized_text(
        compact(
            previous_profile.get("product_anchor")
            or previous_profile.get("product_name")
        )
    )
    if current_product and previous_product and current_product == previous_product:
        current_year_set = project_years(current)
        previous_year_set = project_years(previous)
        if (
            current_year_set
            and previous_year_set
            and current_year_set.isdisjoint(previous_year_set)
        ):
            return 0.48
        return 0.95

    current_event = normalized_text(compact(current_profile.get("event_anchor")))
    previous_event = normalized_text(compact(previous_profile.get("event_anchor")))
    bonus = 0.0
    if current_event and previous_event:
        bonus = (
            0.55
            if current_event == previous_event
            else 0.35 * jaccard(tokens(current_event), tokens(previous_event))
        )

    current_terms, _ = profile_terms(current)
    previous_terms, _ = profile_terms(previous)
    lexical = jaccard(current_terms, previous_terms)
    score = min(1.0, bonus + (0.65 * lexical))

    current_locations = {
        normalized_text(str(value))
        for value in (current_profile.get("locations") or [])
        if str(value).strip()
    }
    previous_locations = {
        normalized_text(str(value))
        for value in (previous_profile.get("locations") or [])
        if str(value).strip()
    }
    shared_distinctive = {
        token
        for token in current_terms.intersection(previous_terms)
        if len(token) >= 5
    }
    if shared_distinctive and current_locations.intersection(previous_locations):
        score = max(score, 0.38)

    current_year_set = project_years(current)
    previous_year_set = project_years(previous)
    if (
        current_year_set
        and previous_year_set
        and current_year_set.isdisjoint(previous_year_set)
    ):
        score *= 0.72
    return score


def document_similarity(project: Project, document: CorpusDocument) -> float:
    project_terms, phrases = profile_terms(project)
    body = " ".join(
        filter(
            None,
            [
                document.title,
                document.snippet,
                (document.content or "")[:4000],
            ],
        )
    )
    body_norm = normalized_text(body)
    lexical = jaccard(project_terms, tokens(body))

    anchor = 0.0
    for phrase in phrases:
        phrase_norm = normalized_text(phrase)
        if phrase_norm and phrase_norm in body_norm:
            anchor = max(anchor, 0.72)

    topic_norm = normalized_text(project.topic or "")
    if topic_norm and topic_norm in body_norm:
        anchor = max(anchor, 0.82)

    return min(1.0, max(anchor, min(0.70, lexical * 1.55)))

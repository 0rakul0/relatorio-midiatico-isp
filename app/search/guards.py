from __future__ import annotations

import re

from app.models import Project
from app.services.collection.guards import query_preserves_project_anchor
from app.topic_profile import normalized_text


STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma",
}


def query_tokens(query: str) -> set[str]:
    text = re.sub(r"(?:^|\s)site:[^\s]+", " ", query or "")
    text = normalized_text(text)
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in STOPWORDS and len(token) > 1
    }


def semantic_similarity(left: str, right: str) -> float:
    a = query_tokens(left)
    b = query_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_redundant(candidate: str, selected: list[str], threshold: float = 0.84) -> bool:
    normalized = " ".join(normalized_text(candidate).split())
    for previous in selected:
        if normalized == " ".join(normalized_text(previous).split()):
            return True
        if semantic_similarity(candidate, previous) >= threshold:
            return True
    return False


def media_profile_tokens(project: Project) -> set[str]:
    profile = project.topic_profile or {}
    values = [project.topic]
    for key in (
        "product_name", "product_anchor", "event_anchor",
        "product_search_variants", "event_search_variants", "subject_terms",
        "actors", "actions", "locations", "organizations", "search_synonyms",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    tokens: set[str] = set()
    for value in values:
        tokens.update(query_tokens(value))
    return tokens


def media_query_is_acceptable(project: Project, query: str) -> bool:
    if query_preserves_project_anchor(project, query, purpose="MEDIA_REPERCUSSION"):
        return True
    query_set = query_tokens(query)
    profile_set = media_profile_tokens(project)
    if not query_set or not profile_set:
        return False
    overlap = query_set.intersection(profile_set)
    return len(overlap) >= 2 or (
        len(overlap) == 1 and len(query_set) <= 4 and len(profile_set) <= 6
    )


def query_is_acceptable(project: Project, query: str, *, purpose: str) -> bool:
    if purpose == "MEDIA_REPERCUSSION":
        return media_query_is_acceptable(project, query)
    return query_preserves_project_anchor(project, query, purpose=purpose)

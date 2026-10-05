from __future__ import annotations

import re

from app.topics.temporal import normalized_text


KNOWN_LOCATION_ALIASES = {
    "mazuema": "Muzema",
    "muzema": "Muzema",
}


def clean_phrase(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def canonicalize_known_locations(text: str) -> str:
    value = clean_phrase(text)
    if not value:
        return ""

    corrected = value
    for alias, canonical in KNOWN_LOCATION_ALIASES.items():
        corrected = re.sub(
            rf"\b{re.escape(alias)}\b",
            canonical,
            corrected,
            flags=re.IGNORECASE,
        )
    return corrected


def location_topic_variants(topic: str) -> tuple[list[str], list[str]]:
    original = clean_phrase(topic)
    if not original:
        return [], []

    normalized = normalized_text(original)
    locations: list[str] = []

    for alias, canonical in KNOWN_LOCATION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            locations.append(canonical)

    if "rio de janeiro" in normalized or re.search(r"\brj\b", normalized):
        locations.extend(
            ["Rio de Janeiro", "RJ", "estado do Rio de Janeiro"]
        )

    canonical_topic = canonicalize_known_locations(original)
    variants = [canonical_topic]
    if normalized_text(canonical_topic) != normalized_text(original):
        variants.append(original)

    return (
        list(dict.fromkeys(location for location in locations if location)),
        list(dict.fromkeys(variant for variant in variants if variant)),
    )

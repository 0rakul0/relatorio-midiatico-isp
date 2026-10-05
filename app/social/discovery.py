from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MediaItem, Project, SearchHit
from app.social.urls import platform_for_url, url_key


def has_duckduckgo_provenance(item: MediaItem) -> bool:
    source = str(item.search_source or "").strip().lower()
    if source.startswith("duckduckgo"):
        return True
    for entry in item.source_provenance or []:
        if not isinstance(entry, dict):
            continue
        provider = str(
            entry.get("source") or entry.get("provider") or ""
        ).strip().lower()
        if provider.startswith("duckduckgo"):
            return True
    return False


def candidate_urls(
    db: Session,
    project_id: int,
) -> dict[str, list[tuple[str, int | None, str | None]]]:
    found: dict[str, dict[str, tuple[str, int | None, str | None]]] = defaultdict(dict)

    for item in db.scalars(
        select(MediaItem).where(MediaItem.project_id == project_id)
    ).all():
        if not has_duckduckgo_provenance(item):
            continue
        platform = platform_for_url(item.url)
        if not platform:
            continue
        found[platform][url_key(item.url)] = (item.url, item.id, item.title)

    for hit in db.scalars(
        select(SearchHit).where(SearchHit.project_id == project_id)
    ).all():
        if not str(hit.provider or "").strip().lower().startswith("duckduckgo"):
            continue
        platform = platform_for_url(hit.url)
        if not platform:
            continue
        url = str(hit.url or "").strip()
        found[platform].setdefault(
            url_key(url), (url, hit.media_item_id, hit.title)
        )

    limit = max(1, int(get_settings().apify_social_max_posts_per_platform))
    return {
        platform: list(rows.values())[:limit]
        for platform, rows in found.items()
        if rows
    }


def social_discovery_terms(project: Project) -> list[str]:
    profile = project.topic_profile or {}
    values: list[str] = []

    for key in (
        "event_search_variants",
        "product_search_variants",
        "search_synonyms",
        "subject_terms",
        "actions",
    ):
        raw = profile.get(key)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if str(value).strip())

    strategy = profile.get("search_strategy") or {}
    if isinstance(strategy, dict):
        if strategy.get("primary_query"):
            values.append(str(strategy["primary_query"]))
        values.extend(
            str(value)
            for value in (strategy.get("complementary_queries") or [])
            if str(value).strip()
        )

    actors = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("actors") or [])
        if str(value).strip()
    ][:4]
    if len(actors) >= 2:
        values.append(" ".join(actors[:2]))
    values.extend(actors)
    values.append(project.topic)

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" ?.,;:")
        if len(text) < 3:
            continue
        lowered = text.casefold()
        for prefix in (
            "como está ",
            "como esta ",
            "como ficou ",
            "qual é ",
            "qual e ",
            "quais são ",
            "quais sao ",
        ):
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                lowered = text.casefold()
                break
        words = text.split()
        if len(words) > 8:
            text = " ".join(words[:8])
        key = text.casefold()
        if len(text) < 3 or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned


def social_recovery_terms(
    project: Project,
    primary_terms: list[str],
) -> list[str]:
    profile = project.topic_profile or {}
    values: list[str] = []

    for key in ("subject_terms", "actions", "actors", "organizations"):
        raw = profile.get(key)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if str(value).strip())

    for term in primary_terms:
        words = [word for word in term.split() if len(word) > 2]
        if 2 <= len(words) <= 6:
            values.append(" ".join(words[:4]))

    year = str(project.collection_end.year) if project.collection_end else ""
    locations = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("locations") or [])
        if str(value).strip()
    ]
    location = locations[0] if locations else ""

    cleaned: list[str] = []
    seen = {item.casefold() for item in primary_terms}
    for value in values:
        text = " ".join(str(value or "").split()).strip(" ?.,;:")
        if len(text) < 3:
            continue
        words = text.split()
        if len(words) > 5:
            text = " ".join(words[:5])
        if (
            location
            and location.casefold() not in text.casefold()
            and len(text.split()) <= 3
        ):
            text = f"{text} {location}".strip()
        if year and year not in text and len(text.split()) <= 4:
            text = f"{text} {year}".strip()
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned

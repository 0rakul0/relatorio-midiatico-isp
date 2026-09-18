"""Persist raw search hits without discarding evidence during collection.

Collection has one job: preserve what the search providers returned. Relevance,
window eligibility and factual/media validity are decided later. Every provider
row becomes a ``SearchHit``. Valid HTTP(S) URLs are also consolidated into a
single ``MediaItem`` per canonical URL so downstream LLM stages do not pay to
process the same article repeatedly.

Technical checks may add flags and may decide whether a provider result is
usable to stop the DuckDuckGo -> Tavily fallback, but they never delete the raw
hit from the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.cost_tracker import current_run_id
from app.models import MediaItem, Project, SearchHit, SearchQuery
from app.services.collection.common import (
    _append_purpose,
    _site_domain_from_query,
    canonicalize,
    record_source_provenance,
    result_publication_date,
)
from app.services.collection.guards import collection_guard
from app.services.collection.youtube_helpers import (
    is_youtube_url,
    matches_priority_youtube_channel,
)


_HARD_USABILITY_FLAGS = {
    "INVALID_URL",
    "DOMAIN_MISMATCH",
    "NON_YOUTUBE_RESULT",
    "PRIORITY_CHANNEL_MISMATCH",
    "OUTSIDE_COLLECTION_WINDOW",
    "COLLECTION_GUARD_MISMATCH",
}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _valid_http_url(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    parsed = urlparse(url)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc)


def _hit_status(flags: list[str]) -> str:
    if "INVALID_URL" in flags:
        return "INVALID"
    return "FLAGGED" if flags else "COLLECTED"


def _provider_usable(flags: list[str]) -> bool:
    return not _HARD_USABILITY_FLAGS.intersection(flags)


def _persist_search_hit(
    db: Session,
    *,
    project: Project,
    query: SearchQuery | None,
    provider: str,
    row: dict[str, Any],
    purpose: str,
    flags: list[str],
    canonical_url: str | None,
    domain: str | None,
    published_at: date | None,
    source_name: str | None,
    media_item_id: int | None,
    target: str | None = None,
    view_count: int | None = None,
) -> SearchHit:
    hit = SearchHit(
        project_id=project.id,
        run_id=current_run_id(),
        search_query_id=query.id if query else None,
        media_item_id=media_item_id,
        provider=provider,
        purpose=purpose,
        query=query.query if query else None,
        target=target,
        title=str(row.get("title") or "").strip() or None,
        url=str(row.get("url") or "").strip() or None,
        canonical_url=canonical_url,
        domain=domain,
        published_at=published_at,
        published_at_raw=(
            str(row.get("published_at")) if row.get("published_at") not in (None, "") else None
        ),
        snippet=row.get("snippet"),
        content=row.get("content"),
        source_name=source_name,
        view_count=view_count,
        technical_status=_hit_status(flags),
        technical_flags=list(dict.fromkeys(flags)),
        raw_payload=_json_safe(dict(row)),
        retrieved_at=datetime.now(timezone.utc),
    )
    db.add(hit)
    return hit


def _consolidate_media_item(
    db: Session,
    *,
    project: Project,
    query: SearchQuery | None,
    existing_items: dict[str, MediaItem],
    row: dict[str, Any],
    canonical: str,
    host: str,
    published_at: date | None,
    source_name: str,
    provider: str,
    purpose: str,
    view_count: int | None = None,
    target: str | None = None,
) -> tuple[MediaItem, bool]:
    """Return ``(media_item, is_duplicate)`` after deterministic URL consolidation."""
    snippet = row.get("snippet")
    content = row.get("content")
    title = str(row.get("title") or "Sem titulo").strip() or "Sem titulo"
    url = str(row.get("url") or "").strip()

    existing = existing_items.get(canonical)
    if existing is not None:
        _append_purpose(existing, purpose)
        record_source_provenance(
            existing,
            source=provider,
            title=title,
            url=url,
            published_at=row.get("published_at"),
            snippet=snippet,
            channel=source_name,
            view_count=view_count,
            query=query.query if query else None,
            target=target,
        )
        if not existing.snippet and snippet:
            existing.snippet = snippet
        if not existing.content and content:
            existing.content = content
        if not existing.published_at and published_at:
            existing.published_at = published_at
        if not existing.source_name and source_name:
            existing.source_name = source_name
        if existing.view_count is None and view_count is not None:
            existing.view_count = view_count
        return existing, True

    item = MediaItem(
        project_id=project.id,
        query_id=query.id if query else None,
        title=title,
        url=url,
        canonical_url=canonical,
        domain=host,
        published_at=published_at,
        snippet=snippet,
        content=content or snippet or "",
        source_name=source_name,
        view_count=view_count,
        search_source=provider,
        source_provenance=[
            {
                "source": provider,
                "title": title,
                "url": url,
                "published_at": _json_safe(row.get("published_at")),
                "snippet": snippet,
                "channel": source_name,
                "view_count": view_count,
                "query": query.query if query else None,
                "target": target,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
        discovery_purposes=[purpose],
        status="PENDING",
        fact_status="PENDING",
    )
    db.add(item)
    db.flush()  # SearchHit links to the consolidated MediaItem immediately.
    existing_items[canonical] = item
    return item, False


def persist_web_rows(
    db: Session,
    project: Project,
    query: SearchQuery | None,
    existing_items: dict[str, MediaItem],
    rows: list[dict[str, Any]],
    *,
    has_window: bool,
    start: date | None = None,
    end: date | None = None,
    max_accepted: int | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Persist every web row and return only provider-usable rows for fallback logic.

    ``max_accepted`` no longer limits persistence. It only caps how many usable
    rows are returned to the provider chain, preserving backward compatibility
    with callers while guaranteeing that every provider hit is stored.
    """
    counters = {
        "returned": len(rows),
        "persisted": 0,
        "added": 0,
        "duplicates": 0,
        "flagged": 0,
        "invalid": 0,
        "usable": 0,
        "rejected": 0,  # backward-compatible field: collection no longer discards hits
    }
    site_domain = _site_domain_from_query(query.query) if query else None
    is_youtube_query = bool(query and query.kind == "youtube")
    purpose = str(query.purpose if query else "MEDIA_REPERCUSSION")
    usable_rows: list[dict[str, Any]] = []

    for row in rows:
        url = str(row.get("url") or "").strip()
        provider = str(row.get("provider") or "duckduckgo")
        flags: list[str] = []
        canonical: str | None = None
        host: str | None = None
        published_at = result_publication_date(row.get("published_at"))

        if not _valid_http_url(url):
            flags.append("INVALID_URL")
            counters["invalid"] += 1
        else:
            host = urlparse(url).netloc.lower().split(":")[0]
            canonical = canonicalize(url)

            if site_domain and not (host == site_domain or host.endswith("." + site_domain)):
                flags.append("DOMAIN_MISMATCH")

            if has_window and published_at and start and end and not (start <= published_at <= end):
                flags.append("OUTSIDE_COLLECTION_WINDOW")

            if is_youtube_query and not is_youtube_url(url):
                flags.append("NON_YOUTUBE_RESULT")

            if purpose == "MEDIA_REPERCUSSION" and not collection_guard(
                project,
                title=row.get("title"),
                snippet=row.get("snippet"),
                content=row.get("content") or row.get("snippet") or "",
            ):
                flags.append("COLLECTION_GUARD_MISMATCH")

        source_name = str(row.get("source_name") or host or "Fonte nao identificada")
        media_item: MediaItem | None = None
        if canonical and host:
            media_item, duplicate = _consolidate_media_item(
                db,
                project=project,
                query=query,
                existing_items=existing_items,
                row=row,
                canonical=canonical,
                host=host,
                published_at=published_at,
                source_name=source_name,
                provider=provider,
                purpose=purpose,
            )
            if duplicate:
                counters["duplicates"] += 1
                flags.append("DUPLICATE_URL")
            else:
                counters["added"] += 1

        _persist_search_hit(
            db,
            project=project,
            query=query,
            provider=provider,
            row=row,
            purpose=purpose,
            flags=flags,
            canonical_url=canonical,
            domain=host,
            published_at=published_at,
            source_name=source_name,
            media_item_id=media_item.id if media_item else None,
        )
        counters["persisted"] += 1
        if flags:
            counters["flagged"] += 1

        if _provider_usable(flags):
            if max_accepted is None or len(usable_rows) < max_accepted:
                usable_rows.append({**row, "source_name": source_name, "provider": provider})
                counters["usable"] += 1

    if progress_detail:
        progress_detail(
            "Coleta preservada: "
            f"{counters['returned']} hit(s), {counters['persisted']} armazenado(s), "
            f"{counters['added']} URL(s) unica(s), {counters['duplicates']} duplicado(s), "
            f"{counters['flagged']} sinalizado(s); nenhum hit descartado por relevancia"
        )

    return counters, usable_rows


def persist_video_rows(
    db: Session,
    project: Project,
    task,
    existing_items: dict[str, MediaItem],
    rows: list[dict[str, Any]],
    *,
    has_window: bool,
    start: date | None = None,
    end: date | None = None,
    max_accepted: int | None = None,
    enforce_priority_channel: bool = True,
    progress_detail: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Persist every video-search row; channel/window/theme mismatches become flags."""
    counters = {
        "returned": len(rows),
        "persisted": 0,
        "added": 0,
        "duplicates": 0,
        "flagged": 0,
        "invalid": 0,
        "usable": 0,
        "rejected": 0,
    }
    purpose = "MEDIA_REPERCUSSION"
    usable_rows: list[dict[str, Any]] = []

    for row in rows:
        url = str(row.get("url") or "").strip()
        channel = str(row.get("source_name") or "").strip() or None
        provider = str(row.get("provider") or "duckduckgo_video")
        published_at = result_publication_date(row.get("published_at"))
        description = row.get("content") or row.get("snippet") or None
        title = str(row.get("title") or "Video sem titulo")
        flags: list[str] = []
        canonical: str | None = None
        host: str | None = None

        raw_view_count = row.get("view_count")
        view_count = (
            raw_view_count
            if isinstance(raw_view_count, int)
            and not isinstance(raw_view_count, bool)
            and raw_view_count >= 0
            else None
        )

        if not _valid_http_url(url):
            flags.append("INVALID_URL")
            counters["invalid"] += 1
        else:
            host = urlparse(url).netloc.lower().split(":")[0]
            canonical = canonicalize(url)
            if not is_youtube_url(url):
                flags.append("NON_YOUTUBE_RESULT")
            if (
                task.is_priority
                and enforce_priority_channel
                and not matches_priority_youtube_channel(channel, task.target)
            ):
                flags.append("PRIORITY_CHANNEL_MISMATCH")
            if has_window and published_at and start and end and not (start <= published_at <= end):
                flags.append("OUTSIDE_COLLECTION_WINDOW")
            if not collection_guard(
                project,
                title=title,
                snippet=description,
                content=description,
            ):
                flags.append("COLLECTION_GUARD_MISMATCH")

        source_name = channel or host or "Fonte de video nao identificada"
        media_item: MediaItem | None = None
        normalized_row = {**row, "title": title, "content": description, "snippet": description}
        if canonical and host:
            media_item, duplicate = _consolidate_media_item(
                db,
                project=project,
                query=None,
                existing_items=existing_items,
                row=normalized_row,
                canonical=canonical,
                host=host,
                published_at=published_at,
                source_name=source_name,
                provider=provider,
                purpose=purpose,
                view_count=view_count,
                target=task.target,
            )
            if duplicate:
                counters["duplicates"] += 1
                flags.append("DUPLICATE_URL")
            else:
                counters["added"] += 1

        _persist_search_hit(
            db,
            project=project,
            query=None,
            provider=provider,
            row=normalized_row,
            purpose=purpose,
            flags=flags,
            canonical_url=canonical,
            domain=host,
            published_at=published_at,
            source_name=source_name,
            media_item_id=media_item.id if media_item else None,
            target=task.target,
            view_count=view_count,
        )
        counters["persisted"] += 1
        if flags:
            counters["flagged"] += 1

        if _provider_usable(flags):
            if max_accepted is None or len(usable_rows) < max_accepted:
                usable_rows.append({**normalized_row, "source_name": source_name, "provider": provider})
                counters["usable"] += 1

    if progress_detail:
        progress_detail(
            "Coleta de video preservada: "
            f"{counters['returned']} hit(s), {counters['persisted']} armazenado(s), "
            f"{counters['added']} URL(s) unica(s), {counters['duplicates']} duplicado(s), "
            f"{counters['flagged']} sinalizado(s)"
        )

    return counters, usable_rows

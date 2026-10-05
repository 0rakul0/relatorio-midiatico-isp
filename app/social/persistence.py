from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MediaItem, Project, SocialComment, SocialPost
from app.social.comments import source_url as _source_url
from app.social.dates import resolve_post_datetime as _resolve_post_datetime
from app.social.urls import url_key as _url_key


def _historical_post(db: Session, project_id: int, platform: str, url: str) -> SocialPost | None:
    key = _url_key(url)
    rows = db.scalars(
        select(SocialPost)
        .where(SocialPost.project_id != project_id, SocialPost.platform == platform)
        .order_by(SocialPost.collected_at.desc(), SocialPost.id.desc())
    ).all()
    return next((row for row in rows if _url_key(row.url) == key), None)


def _reuse_historical_comments(
    db: Session,
    project: Project,
    target: SocialPost,
    source: SocialPost,
    existing_ids: set[str],
) -> int:
    copied = 0
    for row in db.scalars(
        select(SocialComment).where(SocialComment.social_post_id == source.id)
    ).all():
        if row.external_id in existing_ids:
            continue
        db.add(SocialComment(
            project_id=project.id,
            social_post_id=target.id,
            platform=row.platform,
            external_id=row.external_id,
            parent_external_id=row.parent_external_id,
            text=row.text,
            published_at=row.published_at,
            like_count=row.like_count,
            reply_count=row.reply_count,
            source_url=target.url,
            collected_at=row.collected_at,
        ))
        existing_ids.add(row.external_id)
        copied += 1
    return copied


def _social_reuse_state(
    db: Session,
    project: Project,
    platform: str,
    url: str,
    target: SocialPost,
    existing_ids: set[str],
) -> tuple[str, int]:
    source = _historical_post(db, project.id, platform, url)
    if source is None:
        return "COLLECT", 0
    copied = _reuse_historical_comments(db, project, target, source, existing_ids)
    if copied == 0:
        return "COLLECT", 0
    collected_at = source.collected_at
    if collected_at is None:
        return "REFRESH", copied
    if collected_at.tzinfo is not None:
        collected_at = collected_at.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ttl = timedelta(days=max(0, int(get_settings().social_reuse_max_age_days)))
    return ("REFRESH" if ttl.days == 0 or now - collected_at > ttl else "REUSE"), copied


def _append_post_date_provenance(
    media: MediaItem | None,
    *,
    published_at: datetime,
    source: str,
) -> None:
    if media is None:
        return
    if media.published_at is None:
        media.published_at = published_at.date()
    provenance = list(media.source_provenance or [])
    if not any(
        isinstance(entry, dict)
        and entry.get("source") == "social_post_date"
        and entry.get("date_source") == source
        and entry.get("published_at") == published_at.isoformat()
        for entry in provenance
    ):
        provenance.append(
            {
                "source": "social_post_date",
                "date_source": source,
                "published_at": published_at.isoformat(),
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        media.source_provenance = provenance


def _enrich_posts_from_apify_dataset(
    db: Session,
    *,
    platform: str,
    dataset: list[dict],
    post_by_url: dict[str, SocialPost],
) -> int:
    updated = 0
    for raw in dataset:
        if not isinstance(raw, dict):
            continue
        source_key = _url_key(_source_url(raw))
        post = post_by_url.get(source_key)
        if post is None and source_key:
            post = next(
                (
                    candidate
                    for key, candidate in post_by_url.items()
                    if source_key.startswith(key) or key.startswith(source_key)
                ),
                None,
            )
        if post is None:
            continue

        media = db.get(MediaItem, post.media_item_id) if post.media_item_id else None
        media_date = media.published_at if media is not None else None
        published_at, date_source = _resolve_post_datetime(
            platform,
            row=raw,
            url=post.url,
            media_published_at=media_date,
        )
        if published_at is None:
            continue

        if post.published_at is None:
            post.published_at = published_at
            updated += 1
        _append_post_date_provenance(
            media,
            published_at=published_at,
            source=date_source or "unknown",
        )
    return updated


def _within_window(project: Project, value: datetime | None) -> bool:
    if value is None or not project.has_custom_date_window:
        return True
    return project.collection_start <= value.date() <= project.collection_end


def _ensure_post(
    db: Session,
    *,
    project: Project,
    platform: str,
    url: str,
    media_item_id: int | None,
    post_text: str | None,
    actor_id: str,
) -> SocialPost:
    post = db.scalar(
        select(SocialPost).where(
            SocialPost.project_id == project.id,
            SocialPost.platform == platform,
            SocialPost.url == url,
        )
    )
    media = db.get(MediaItem, media_item_id) if media_item_id else None
    media_date = media.published_at if media is not None else None
    resolved_date, date_source = _resolve_post_datetime(
        platform,
        url=url,
        media_published_at=media_date,
    )

    if post is None:
        post = SocialPost(
            project_id=project.id,
            media_item_id=media_item_id,
            platform=platform,
            url=url,
            external_id="url:" + sha256(url.encode("utf-8")).hexdigest()[:40],
            post_text=post_text,
            published_at=resolved_date,
            actor_id=actor_id,
        )
        db.add(post)
        db.flush()
    else:
        post.actor_id = actor_id
        if post.media_item_id is None and media_item_id is not None:
            post.media_item_id = media_item_id
        if not post.post_text and post_text:
            post.post_text = post_text
        if post.published_at is None and resolved_date is not None:
            post.published_at = resolved_date

    if resolved_date is not None:
        _append_post_date_provenance(
            media,
            published_at=resolved_date,
            source=date_source or "unknown",
        )
    return post


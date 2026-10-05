from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from typing import Any
from urllib.parse import urlparse

from app.services.collection.common import result_publication_date


def parse_reference_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def relative_duckduckgo_datetime(value: object, *, retrieved_at: object) -> datetime | None:
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return None
    reference = parse_reference_datetime(retrieved_at)
    fixed = {
        "today": timedelta(0), "hoje": timedelta(0),
        "yesterday": timedelta(days=1), "ontem": timedelta(days=1),
    }
    if text in fixed:
        return (reference - fixed[text]).replace(tzinfo=None)
    patterns = (
        (r"^(\d+)\s*(?:minute|minutes|min|mins)\s+ago$", "minutes"),
        (r"^(?:há\s+)?(\d+)\s*(?:minuto|minutos|min)\s*(?:atrás)?$", "minutes"),
        (r"^(\d+)\s*(?:hour|hours|hr|hrs)\s+ago$", "hours"),
        (r"^(?:há\s+)?(\d+)\s*(?:hora|horas|h)\s*(?:atrás)?$", "hours"),
        (r"^(\d+)\s*(?:day|days)\s+ago$", "days"),
        (r"^(?:há\s+)?(\d+)\s*(?:dia|dias)\s*(?:atrás)?$", "days"),
        (r"^(\d+)\s*(?:week|weeks)\s+ago$", "weeks"),
        (r"^(?:há\s+)?(\d+)\s*(?:semana|semanas)\s*(?:atrás)?$", "weeks"),
    )
    for pattern, unit in patterns:
        match = re.match(pattern, text)
        if match:
            return (reference - timedelta(**{unit: int(match.group(1))})).replace(tzinfo=None)
    return None


def duckduckgo_result_date(row: dict) -> tuple[date | None, str | None, str | None]:
    raw = row.get("published_at_raw")
    if raw in (None, ""):
        raw = row.get("published_at")
    raw_text = str(raw).strip() if raw not in (None, "") else None
    explicit = result_publication_date(raw)
    if explicit is not None:
        return explicit, "duckduckgo_metadata", raw_text
    relative = relative_duckduckgo_datetime(raw, retrieved_at=row.get("retrieved_at"))
    if relative is not None:
        return relative.date(), "duckduckgo_relative_date", raw_text
    return None, None, raw_text


def nested(payload: dict, *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first(payload: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = nested(payload, *key.split(".")) if "." in key else payload.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return parse_datetime(float(text))
    for candidate in (text.replace("Z", "+00:00"), text.replace(" ", "T").replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def post_datetime_from_row(platform: str, row: dict) -> tuple[datetime | None, str | None]:
    keys = (
        "postCreatedAt", "post_created_at", "postTimestamp", "post_timestamp",
        "postDate", "post_date", "publicationDate", "publication_date",
        "takenAt", "taken_at", "post.createdAt", "post.created_at",
        "post.timestamp", "post.date", "post.takenAt", "post.taken_at",
        "metadata.sourceTweetCreatedAt", "metadata.source_tweet_created_at",
        "metadata.sourceTweet.created_at", "sourceTweet.createdAt",
        "sourceTweet.created_at", "tweet.createdAt", "tweet.created_at",
    )
    parsed = parse_datetime(first(row, keys))
    return (parsed, "apify_post_metadata") if parsed is not None else (None, None)


def post_datetime_from_url(platform: str, url: str) -> tuple[datetime | None, str | None]:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        return None, None
    try:
        if platform == "x":
            match = re.search(r"/status/(\d+)", path)
            if not match:
                return None, None
            snowflake = int(match.group(1))
            timestamp_ms = (snowflake >> 22) + 1288834974657
            parsed = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            if 2010 <= parsed.year <= datetime.now(timezone.utc).year + 1:
                return parsed, "x_snowflake"
        if platform == "tiktok":
            match = re.search(r"/video/(\d+)", path)
            if not match:
                return None, None
            parsed = datetime.fromtimestamp(int(match.group(1)) >> 32, tz=timezone.utc).replace(tzinfo=None)
            if 2016 <= parsed.year <= datetime.now(timezone.utc).year + 1:
                return parsed, "tiktok_video_id"
    except (OverflowError, OSError, ValueError):
        return None, None
    return None, None


def resolve_post_datetime(platform: str, *, row: dict | None = None, url: str = "", media_published_at: date | None = None) -> tuple[datetime | None, str | None]:
    if row:
        parsed, source = post_datetime_from_row(platform, row)
        if parsed is not None:
            return parsed, source
    if media_published_at is not None:
        return datetime.combine(media_published_at, datetime.min.time()), "duckduckgo_metadata"
    return post_datetime_from_url(platform, url)

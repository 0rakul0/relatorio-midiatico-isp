from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from app.social.dates import first, parse_datetime


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if value is None or value == "":
            return None
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def source_url(row: dict) -> str:
    value = first(row, (
        "postUrl", "postURL", "post_url", "inputUrl", "sourceUrl",
        "facebookUrl", "facebook_url", "videoWebUrl", "webVideoUrl",
        "metadata.sourceTweetUrl", "metadata.source_tweet_url",
    ))
    return str(value or "").strip()


def external_id(platform: str, row: dict, *, source_url_value: str, text: str, published_at: datetime | None) -> str:
    explicit = first(row, (
        "commentId", "comment_id", "replyId", "cid", "id", "pk",
        "tweet_id", "tweetId", "legacy.id_str",
    ))
    if explicit:
        return str(explicit)[:250]
    material = f"{platform}|{source_url_value}|{published_at}|{text}".encode("utf-8", errors="ignore")
    return "hash:" + sha256(material).hexdigest()[:48]


def normalize_comment(platform: str, row: dict, *, parent_external_id: str | None = None) -> dict | None:
    if platform == "x" and bool(row.get("is_source_tweet")):
        return None
    text = first(row, (
        "commentText", "comment_text", "replyText", "text",
        "full_text", "content", "message", "legacy.full_text",
    ))
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return None
    src = source_url(row)
    published_at = parse_datetime(first(row, (
        "timestamp", "date", "createdAt", "created_at", "createTimeISO",
        "create_time_iso", "publishedAt", "legacy.created_at",
    )))
    return {
        "external_id": external_id(platform, row, source_url_value=src, text=text, published_at=published_at),
        "parent_external_id": parent_external_id or (
            str(first(row, (
                "parentCommentId", "parent_comment_id", "repliesToId",
                "replies_to_id", "in_reply_to_status_id_str",
            )) or "").strip() or None
        ),
        "text": text,
        "published_at": published_at,
        "like_count": int_or_none(first(row, (
            "likesCount", "likeCount", "diggCount", "digg_count",
            "favouriteCount", "favoriteCount", "likes", "favorite_count",
            "legacy.favorite_count",
        ))),
        "reply_count": int_or_none(first(row, (
            "repliesCount", "replyCount", "replyCommentTotal",
            "reply_comment_total", "reply_count", "legacy.reply_count",
        ))),
        "source_url": src,
    }


def iter_comments(platform: str, row: dict):
    top = normalize_comment(platform, row)
    parent_id = None
    if top is not None:
        yield top
        parent_id = top["external_id"]
    replies = row.get("replies")
    if not isinstance(replies, list):
        return
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        normalized = normalize_comment(platform, reply, parent_external_id=parent_id)
        if normalized is not None:
            if not normalized["source_url"] and top is not None:
                normalized["source_url"] = top["source_url"]
            yield normalized

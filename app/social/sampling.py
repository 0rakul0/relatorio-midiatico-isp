from __future__ import annotations

from collections import defaultdict
from datetime import timezone

from app.models import SocialComment


def comment_recency_key(comment: SocialComment) -> float:
    value = comment.published_at
    if value is None:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).timestamp()


def mixed_post_comment_order(rows: list[SocialComment]) -> list[SocialComment]:
    if not rows:
        return []
    by_likes = sorted(rows, key=lambda row: ((row.like_count or 0), (row.reply_count or 0), row.id), reverse=True)
    by_replies = sorted(rows, key=lambda row: ((row.reply_count or 0), (row.like_count or 0), row.id), reverse=True)
    by_recent = sorted(rows, key=lambda row: (comment_recency_key(row), row.id), reverse=True)
    by_low_engagement = sorted(rows, key=lambda row: ((row.like_count or 0) + (row.reply_count or 0), comment_recency_key(row), row.id))
    ordered: list[SocialComment] = []
    seen: set[int] = set()
    def add(row: SocialComment) -> None:
        key = int(row.id)
        if key not in seen:
            seen.add(key)
            ordered.append(row)
    for candidates in (by_likes, by_replies, by_recent, by_low_engagement):
        if candidates:
            add(candidates[0])
    remaining = sorted(rows, key=lambda row: ((row.like_count or 0) + (row.reply_count or 0), comment_recency_key(row), row.id), reverse=True)
    for row in remaining:
        add(row)
    return ordered


def balanced_sample(comments: list[SocialComment], limit: int) -> list[SocialComment]:
    grouped: dict[str, dict[int, list[SocialComment]]] = defaultdict(lambda: defaultdict(list))
    for comment in comments:
        grouped[comment.platform][int(comment.social_post_id)].append(comment)
    queues = {platform: {post_id: mixed_post_comment_order(rows) for post_id, rows in posts.items()} for platform, posts in grouped.items()}
    post_order = {platform: sorted(posts) for platform, posts in grouped.items()}
    active_platforms = sorted(post_order)
    post_cursor = {platform: 0 for platform in active_platforms}
    output: list[SocialComment] = []
    platform_cursor = 0
    while active_platforms and len(output) < max(0, int(limit)):
        platform = active_platforms[platform_cursor % len(active_platforms)]
        available_posts = [post_id for post_id in post_order[platform] if queues[platform].get(post_id)]
        if not available_posts:
            active_platforms.remove(platform)
            post_cursor.pop(platform, None)
            platform_cursor = 0
            continue
        cursor = post_cursor[platform] % len(available_posts)
        post_id = available_posts[cursor]
        output.append(queues[platform][post_id].pop(0))
        post_cursor[platform] = (cursor + 1) % max(1, len(available_posts))
        platform_cursor += 1
    return output

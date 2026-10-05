from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MediaItem, SocialAnalysis, SocialComment, SocialPost
from app.social.dates import resolve_post_datetime
from app.social.methodology import METHODOLOGY_NOTE


def social_repercussion_for_report(db: Session, project_id: int) -> dict:
    analysis = db.scalar(
        select(SocialAnalysis).where(SocialAnalysis.project_id == project_id)
    )
    posts = list(
        db.scalars(
            select(SocialPost)
            .where(SocialPost.project_id == project_id)
            .order_by(SocialPost.platform.asc(), SocialPost.id.asc())
        ).all()
    )
    total_posts = len(posts)
    total_comments = int(
        db.scalar(
            select(func.count(SocialComment.id)).where(
                SocialComment.project_id == project_id
            )
        )
        or 0
    )

    comment_counts = {
        int(post_id): int(count or 0)
        for post_id, count in db.execute(
            select(SocialComment.social_post_id, func.count(SocialComment.id))
            .where(SocialComment.project_id == project_id)
            .group_by(SocialComment.social_post_id)
        ).all()
    }
    media_ids = [post.media_item_id for post in posts if post.media_item_id]
    media_by_id = {
        item.id: item
        for item in (
            db.scalars(select(MediaItem).where(MediaItem.id.in_(media_ids))).all()
            if media_ids
            else []
        )
    }

    post_inventory = []
    for post in posts:
        media = media_by_id.get(post.media_item_id)
        published_at = post.published_at
        date_source = None
        if published_at is None:
            published_at, date_source = resolve_post_datetime(
                post.platform,
                url=post.url,
                media_published_at=(media.published_at if media is not None else None),
            )
        else:
            date_source = "social_post"
        published_label = (
            published_at.date().isoformat() if published_at is not None else None
        )
        post_inventory.append(
            {
                "platform": post.platform,
                "published_at": published_label,
                "title": (
                    (media.title if media is not None else None)
                    or post.post_text
                    or "Post social"
                ),
                "url": post.url,
                "comments_collected": comment_counts.get(post.id, 0),
                "like_count": post.like_count,
                "share_count": post.share_count,
                "discovery_source": (
                    media.search_source
                    if media is not None and media.search_source
                    else "monitoramento social"
                ),
                "collector": "Apify" if post.actor_id else None,
                "published_at_source": date_source,
            }
        )

    base = {
        "posts": total_posts,
        "comments": total_comments,
        "post_inventory": post_inventory,
    }
    if analysis is None:
        return {
            **base,
            "status": "NOT_ANALYZED",
            "analyzed_comments": 0,
            "platform_counts": {},
            "sentiment_counts": {},
            "emotion_counts": {},
            "position_counts": {},
            "themes": [],
            "discourse_analysis": {},
            "summary": None,
            "methodology_note": METHODOLOGY_NOTE,
        }

    return {
        **base,
        "status": analysis.status,
        "analyzed_comments": int(analysis.analyzed_comments or 0),
        "platform_counts": dict(analysis.platform_counts or {}),
        "sentiment_counts": dict(analysis.sentiment_counts or {}),
        "emotion_counts": dict(analysis.emotion_counts or {}),
        "position_counts": dict(analysis.position_counts or {}),
        "themes": list(analysis.themes or []),
        "discourse_analysis": dict(analysis.discourse_analysis or {}),
        "summary": analysis.summary,
        "methodology_note": analysis.methodology_note or METHODOLOGY_NOTE,
    }

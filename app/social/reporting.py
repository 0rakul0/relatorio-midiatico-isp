from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MediaItem, Project, SocialAnalysis, SocialComment, SocialPost
from app.social.discovery import social_topic_relevance
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

    project = db.get(Project, project_id)
    original_total_posts = len(posts)
    if project is not None:
        posts = [
            post for post in posts
            if social_topic_relevance(
                project,
                media_by_id[post.media_item_id].title if post.media_item_id in media_by_id else post.post_text,
                media_by_id[post.media_item_id].snippet if post.media_item_id in media_by_id else None,
            )[0]
        ]
    excluded_posts = original_total_posts - len(posts)
    included_ids = {post.id for post in posts}
    total_posts = len(posts)
    total_comments = sum(comment_counts.get(post.id, 0) for post in posts)
    comment_counts = {post_id: count for post_id, count in comment_counts.items() if post_id in included_ids}

    post_inventory = []
    known_view_count = 0
    total_view_count = 0
    platform_view_counts: dict[str, int] = {}
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
        effective_views = (
            post.view_count
            if post.view_count is not None
            else (media.view_count if media is not None else None)
        )
        view_source = (
            post.view_count_source
            if post.view_count is not None
            else ("media_item" if effective_views is not None else None)
        )
        if effective_views is not None:
            known_view_count += 1
            total_view_count += int(effective_views or 0)
            platform_view_counts[post.platform] = (
                platform_view_counts.get(post.platform, 0)
                + int(effective_views or 0)
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
                "view_count": effective_views,
                "view_count_source": view_source,
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
        "excluded_posts": excluded_posts,
        "comments": total_comments,
        "post_inventory": post_inventory,
        "view_count_total": total_view_count,
        "view_count_known_posts": known_view_count,
        "view_count_missing_posts": total_posts - known_view_count,
        "platform_view_counts": platform_view_counts,
        "view_count_note": (
            "Soma bruta das visualizações reportadas pelas plataformas/coletor nos posts com métrica disponível. "
            "Não representa pessoas únicas e pode conter sobreposição de audiência entre posts e plataformas."
        ),
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

    # Analises anteriores a filtragem podem estar contaminadas. Exigir
    # reanalise para exibir narrativas e percentuais historicos.
    if excluded_posts and (
        int(analysis.total_posts or 0) != total_posts
        or int(analysis.total_comments or 0) != total_comments
    ):
        return {
            **base,
            "status": "REANALYSIS_REQUIRED",
            "analyzed_comments": 0,
            "platform_counts": {},
            "sentiment_counts": {},
            "emotion_counts": {},
            "position_counts": {},
            "themes": [],
            "discourse_analysis": {},
            "summary": "Amostra social filtrada por relevancia. Reexecute a analise para atualizar comentarios e narrativas.",
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

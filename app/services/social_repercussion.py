from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import cost_context
from app.llm import llm_is_configured
from app.models import (
    MediaItem,
    Project,
    SearchHit,
    SocialAnalysis,
    SocialComment,
    SocialPost,
)
from app.schemas import SocialCommentBatchResponse
from app.tools.providers.apify_social import ApifyUnavailable, run_actor_dataset


METHODOLOGY_NOTE = (
    "A camada social descreve apenas comentarios publicamente visiveis nos "
    "posts localizados na amostra. Ela nao e pesquisa amostral da populacao "
    "e nao deve ser interpretada como opiniao publica do Estado do Rio de Janeiro."
)


def _platform_for_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.lower()
    query = parse_qs(parsed.query)

    if host.endswith("instagram.com") and (
        "/p/" in path or "/reel/" in path or "/reels/" in path
    ):
        return "instagram"

    if host.endswith("facebook.com") or host.endswith("fb.watch"):
        if (
            "/posts/" in path
            or "/reel/" in path
            or "/reels/" in path
            or "/videos/" in path
            or "/share/" in path
            or "story_fbid" in query
            or host.endswith("fb.watch")
        ):
            return "facebook"

    if (
        host == "x.com"
        or host.endswith(".x.com")
        or host.endswith("twitter.com")
    ) and "/status/" in path:
        return "x"
    return None


def _url_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/")
        return f"{host}{path}".lower()
    except Exception:
        return raw.rstrip("/").lower()


def _candidate_urls(
    db: Session,
    project_id: int,
) -> dict[str, list[tuple[str, int | None, str | None]]]:
    found: dict[str, dict[str, tuple[str, int | None, str | None]]] = defaultdict(dict)

    for item in db.scalars(
        select(MediaItem).where(MediaItem.project_id == project_id)
    ).all():
        platform = _platform_for_url(item.url)
        if not platform:
            continue
        key = _url_key(item.url)
        found[platform][key] = (item.url, item.id, item.title)

    for hit in db.scalars(
        select(SearchHit).where(SearchHit.project_id == project_id)
    ).all():
        platform = _platform_for_url(hit.url)
        if not platform:
            continue
        url = str(hit.url or "").strip()
        key = _url_key(url)
        found[platform].setdefault(key, (url, hit.media_item_id, hit.title))

    limit = max(1, int(get_settings().apify_social_max_posts_per_platform))
    return {
        platform: list(rows.values())[:limit]
        for platform, rows in found.items()
        if rows
    }


def _actor_for(platform: str) -> str | None:
    settings = get_settings()
    return {
        "instagram": settings.apify_instagram_comments_actor_id,
        "facebook": settings.apify_facebook_comments_actor_id,
        "x": settings.apify_x_comments_actor_id,
    }.get(platform)


def _actor_input(platform: str, urls: list[str], limit: int) -> dict:
    if platform == "instagram":
        return {"directUrls": urls, "resultsLimit": limit}
    if platform == "facebook":
        return {
            "startUrls": [{"url": url} for url in urls],
            "resultsLimit": limit,
        }
    if platform == "x":
        return {
            "urls": urls,
            "category": "replies",
            "resultsPerCategory": limit,
            "scrapeAll": True,
        }
    raise ValueError(f"Plataforma social desconhecida: {platform}")


def _nested(payload: dict, *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first(payload: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = _nested(payload, *key.split(".")) if "." in key else payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [
        text.replace("Z", "+00:00"),
        text.replace(" ", "T").replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if value is None or value == "":
            return None
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _source_url(row: dict) -> str:
    value = _first(
        row,
        (
            "postUrl",
            "postURL",
            "post_url",
            "inputUrl",
            "sourceUrl",
            "facebookUrl",
            "facebook_url",
            "metadata.sourceTweetUrl",
            "metadata.source_tweet_url",
        ),
    )
    return str(value or "").strip()


def _external_id(
    platform: str,
    row: dict,
    *,
    source_url: str,
    text: str,
    published_at: datetime | None,
) -> str:
    explicit = _first(
        row,
        (
            "commentId",
            "comment_id",
            "id",
            "pk",
            "tweet_id",
            "tweetId",
            "legacy.id_str",
        ),
    )
    if explicit:
        return str(explicit)[:250]
    material = (
        f"{platform}|{source_url}|{published_at}|{text}"
        .encode("utf-8", errors="ignore")
    )
    return "hash:" + sha256(material).hexdigest()[:48]


def _normalize_comment(
    platform: str,
    row: dict,
    *,
    parent_external_id: str | None = None,
) -> dict | None:
    if platform == "x" and bool(row.get("is_source_tweet")):
        return None

    text = _first(
        row,
        (
            "commentText",
            "comment_text",
            "text",
            "full_text",
            "content",
            "message",
            "legacy.full_text",
        ),
    )
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return None

    source_url = _source_url(row)
    published_at = _parse_datetime(
        _first(
            row,
            (
                "timestamp",
                "date",
                "createdAt",
                "created_at",
                "publishedAt",
                "legacy.created_at",
            ),
        )
    )
    return {
        "external_id": _external_id(
            platform,
            row,
            source_url=source_url,
            text=text,
            published_at=published_at,
        ),
        "parent_external_id": parent_external_id
        or (
            str(
                _first(
                    row,
                    (
                        "parentCommentId",
                        "parent_comment_id",
                        "in_reply_to_status_id_str",
                    ),
                )
                or ""
            ).strip()
            or None
        ),
        "text": text,
        "published_at": published_at,
        "like_count": _int_or_none(
            _first(
                row,
                (
                    "likesCount",
                    "likeCount",
                    "likes",
                    "favorite_count",
                    "favoriteCount",
                    "legacy.favorite_count",
                ),
            )
        ),
        "reply_count": _int_or_none(
            _first(
                row,
                (
                    "repliesCount",
                    "replyCount",
                    "reply_count",
                    "legacy.reply_count",
                ),
            )
        ),
        "source_url": source_url,
    }


def _iter_comments(platform: str, row: dict):
    top = _normalize_comment(platform, row)
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
        normalized = _normalize_comment(
            platform,
            reply,
            parent_external_id=parent_id,
        )
        if normalized is not None:
            if not normalized["source_url"] and top is not None:
                normalized["source_url"] = top["source_url"]
            yield normalized


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
    if post is None:
        post = SocialPost(
            project_id=project.id,
            media_item_id=media_item_id,
            platform=platform,
            url=url,
            external_id="url:" + sha256(url.encode("utf-8")).hexdigest()[:40],
            post_text=post_text,
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
    return post


def _balanced_sample(
    comments: list[SocialComment],
    limit: int,
) -> list[SocialComment]:
    groups: dict[str, list[SocialComment]] = defaultdict(list)
    for comment in comments:
        groups[comment.platform].append(comment)
    for rows in groups.values():
        rows.sort(
            key=lambda row: (row.like_count or 0, row.id),
            reverse=True,
        )

    platforms = sorted(groups)
    output: list[SocialComment] = []
    cursor = 0
    while platforms and len(output) < limit:
        platform = platforms[cursor % len(platforms)]
        rows = groups[platform]
        if rows:
            output.append(rows.pop(0))
        if not rows:
            platforms.remove(platform)
            cursor = 0
        else:
            cursor += 1
    return output


def _human_summary(
    analyzed: int,
    sentiment: Counter,
    emotion: Counter,
    position: Counter,
    themes: Counter,
) -> str:
    if analyzed <= 0:
        return "Comentarios coletados, mas sem classificacao semantica disponivel."

    def top(counter: Counter, fallback: str) -> str:
        return str(counter.most_common(1)[0][0]) if counter else fallback

    summary = (
        f"Na amostra de {analyzed} comentario(s) classificado(s), "
        f"o sentimento mais frequente foi {top(sentiment, 'nao identificado').lower()}, "
        f"a emocao mais frequente foi {top(emotion, 'nao identificada').lower()} "
        f"e a posicao mais frequente foi {top(position, 'nao identificada').lower()}."
    )
    top_themes = ", ".join(label for label, _count in themes.most_common(5))
    if top_themes:
        summary += f" Temas recorrentes: {top_themes}."
    return summary


def analyze_social_comments(db: Session, project: Project) -> dict:
    settings = get_settings()
    comments = list(
        db.scalars(
            select(SocialComment)
            .where(SocialComment.project_id == project.id)
            .order_by(SocialComment.like_count.desc(), SocialComment.id.asc())
        ).all()
    )
    posts = list(
        db.scalars(
            select(SocialPost).where(SocialPost.project_id == project.id)
        ).all()
    )
    platform_counts = dict(Counter(comment.platform for comment in comments))

    analysis = db.scalar(
        select(SocialAnalysis).where(SocialAnalysis.project_id == project.id)
    )
    if analysis is None:
        analysis = SocialAnalysis(
            project_id=project.id,
            methodology_note=METHODOLOGY_NOTE,
        )
        db.add(analysis)

    analysis.total_posts = len(posts)
    analysis.total_comments = len(comments)
    analysis.platform_counts = platform_counts
    analysis.methodology_note = METHODOLOGY_NOTE
    analysis.generated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if not comments:
        analysis.status = "NO_COMMENTS"
        analysis.analyzed_comments = 0
        analysis.sentiment_counts = {}
        analysis.emotion_counts = {}
        analysis.position_counts = {}
        analysis.themes = []
        analysis.summary = "Nenhum comentario publico foi recuperado na amostra."
        db.commit()
        return social_repercussion_for_report(db, project.id)

    if not llm_is_configured():
        analysis.status = "COLLECTED_ONLY"
        analysis.analyzed_comments = 0
        analysis.sentiment_counts = {}
        analysis.emotion_counts = {}
        analysis.position_counts = {}
        analysis.themes = []
        analysis.summary = (
            "Comentarios coletados; classificacao semantica indisponivel sem LLM."
        )
        db.commit()
        return social_repercussion_for_report(db, project.id)

    sample = _balanced_sample(
        comments,
        max(1, int(settings.social_analysis_max_comments)),
    )
    batch_size = max(5, int(settings.social_analysis_batch_size))
    sentiment: Counter = Counter()
    emotion: Counter = Counter()
    position: Counter = Counter()
    themes: Counter = Counter()
    analyzed_indices: set[int] = set()

    indexed = list(enumerate(sample))
    with cost_context(
        project_id=project.id,
        operation="social_comment_analysis",
        schema_name="social_comment_analysis_v1",
    ):
        for offset in range(0, len(indexed), batch_size):
            batch = indexed[offset : offset + batch_size]
            payload = {
                "project": {
                    "topic": project.topic,
                    "collection_start": project.collection_start.isoformat(),
                    "collection_end": project.collection_end.isoformat(),
                },
                "methodology": METHODOLOGY_NOTE,
                "comments": [
                    {
                        "index": index,
                        "platform": comment.platform,
                        "text": comment.text[:700],
                        "like_count": comment.like_count,
                    }
                    for index, comment in batch
                ],
            }
            result = get_report_agent().run(
                task="social_comment_analysis",
                payload=payload,
                response_model=SocialCommentBatchResponse,
                schema_name="social_comment_analysis_v1",
                max_output_tokens=3500,
            )
            valid_indices = {index for index, _comment in batch}
            for item in result.get("assessments") or []:
                index = int(item.get("index", -1))
                if index not in valid_indices or index in analyzed_indices:
                    continue
                analyzed_indices.add(index)
                sentiment[str(item.get("sentiment") or "NEUTRO")] += 1
                emotion[str(item.get("emotion") or "NAO_IDENTIFICAVEL")] += 1
                position[str(item.get("position") or "NAO_IDENTIFICAVEL")] += 1
                for theme in item.get("themes") or []:
                    cleaned = " ".join(str(theme).split()).strip().lower()
                    if 2 <= len(cleaned) <= 80:
                        themes[cleaned] += 1

    analysis.status = "COMPLETED" if analyzed_indices else "COLLECTED_ONLY"
    analysis.analyzed_comments = len(analyzed_indices)
    analysis.sentiment_counts = dict(sentiment)
    analysis.emotion_counts = dict(emotion)
    analysis.position_counts = dict(position)
    analysis.themes = [
        {"theme": theme, "count": count}
        for theme, count in themes.most_common(12)
    ]
    analysis.summary = _human_summary(
        len(analyzed_indices),
        sentiment,
        emotion,
        position,
        themes,
    )
    db.commit()
    return social_repercussion_for_report(db, project.id)


def collect_social_repercussion(db: Session, project: Project) -> dict:
    settings = get_settings()
    if not settings.apify_social_enabled:
        return {
            "status": "DISABLED",
            "reason": "APIFY_SOCIAL_ENABLED=false",
            "posts": 0,
            "comments": 0,
            "platforms": {},
            "methodology_note": METHODOLOGY_NOTE,
        }
    if not settings.apify_api_token:
        return {
            "status": "NOT_CONFIGURED",
            "reason": "APIFY_API_TOKEN nao configurado",
            "posts": 0,
            "comments": 0,
            "platforms": {},
            "methodology_note": METHODOLOGY_NOTE,
        }

    candidates = _candidate_urls(db, project.id)
    if not candidates:
        return {
            "status": "NO_POSTS",
            "reason": (
                "DuckDuckGo nao localizou posts sociais publicos elegiveis "
                "na amostra"
            ),
            "posts": 0,
            "comments": 0,
            "platforms": {},
            "methodology_note": METHODOLOGY_NOTE,
        }

    existing_ids = set(
        db.scalars(
            select(SocialComment.external_id).where(
                SocialComment.project_id == project.id
            )
        ).all()
    )
    platform_result: dict[str, dict] = {}
    new_comments = 0
    actor_runs = 0

    for platform, rows in candidates.items():
        actor_id = _actor_for(platform)
        if not actor_id:
            platform_result[platform] = {
                "status": "NOT_CONFIGURED",
                "posts": len(rows),
                "comments": 0,
            }
            continue

        urls = [row[0] for row in rows]
        post_by_url: dict[str, SocialPost] = {}
        for url, media_item_id, title in rows:
            post = _ensure_post(
                db,
                project=project,
                platform=platform,
                url=url,
                media_item_id=media_item_id,
                post_text=title,
                actor_id=actor_id,
            )
            post_by_url[_url_key(url)] = post
        db.flush()

        try:
            dataset = run_actor_dataset(
                actor_id=actor_id,
                token=settings.apify_api_token,
                payload=_actor_input(
                    platform,
                    urls,
                    int(settings.apify_social_comments_per_post),
                ),
                base_url=settings.apify_api_base_url,
                timeout_seconds=settings.apify_social_timeout_seconds,
            )
            actor_runs += 1
        except ApifyUnavailable as exc:
            platform_result[platform] = {
                "status": "ERROR",
                "posts": len(rows),
                "comments": 0,
                "error": str(exc)[:800],
            }
            continue

        accepted = 0
        sole_post = next(iter(post_by_url.values())) if len(post_by_url) == 1 else None
        for raw in dataset:
            for normalized in _iter_comments(platform, raw):
                if not _within_window(project, normalized["published_at"]):
                    continue

                source_key = _url_key(normalized["source_url"])
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
                    post = sole_post
                if post is None:
                    continue

                external_id = normalized["external_id"]
                if external_id in existing_ids:
                    continue
                db.add(
                    SocialComment(
                        project_id=project.id,
                        social_post_id=post.id,
                        platform=platform,
                        external_id=external_id,
                        parent_external_id=normalized["parent_external_id"],
                        text=normalized["text"],
                        published_at=normalized["published_at"],
                        like_count=normalized["like_count"],
                        reply_count=normalized["reply_count"],
                        source_url=post.url,
                    )
                )
                existing_ids.add(external_id)
                accepted += 1
                new_comments += 1

        platform_result[platform] = {
            "status": "OK",
            "posts": len(rows),
            "returned": len(dataset),
            "comments": accepted,
            "actor_id": actor_id,
        }

    db.commit()
    report = analyze_social_comments(db, project)
    report["collection"] = {
        "actor_runs": actor_runs,
        "new_comments": new_comments,
        "platforms": platform_result,
    }
    return report


def social_repercussion_for_report(db: Session, project_id: int) -> dict:
    analysis = db.scalar(
        select(SocialAnalysis).where(SocialAnalysis.project_id == project_id)
    )
    total_posts = int(
        db.scalar(
            select(func.count(SocialPost.id)).where(
                SocialPost.project_id == project_id
            )
        )
        or 0
    )
    total_comments = int(
        db.scalar(
            select(func.count(SocialComment.id)).where(
                SocialComment.project_id == project_id
            )
        )
        or 0
    )
    if analysis is None:
        return {
            "status": "NOT_ANALYZED",
            "posts": total_posts,
            "comments": total_comments,
            "analyzed_comments": 0,
            "platform_counts": {},
            "sentiment_counts": {},
            "emotion_counts": {},
            "position_counts": {},
            "themes": [],
            "summary": None,
            "methodology_note": METHODOLOGY_NOTE,
        }

    return {
        "status": analysis.status,
        "posts": total_posts,
        "comments": total_comments,
        "analyzed_comments": int(analysis.analyzed_comments or 0),
        "platform_counts": dict(analysis.platform_counts or {}),
        "sentiment_counts": dict(analysis.sentiment_counts or {}),
        "emotion_counts": dict(analysis.emotion_counts or {}),
        "position_counts": dict(analysis.position_counts or {}),
        "themes": list(analysis.themes or []),
        "summary": analysis.summary,
        "methodology_note": analysis.methodology_note or METHODOLOGY_NOTE,
    }

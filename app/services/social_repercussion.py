from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import current_run_id
from app.llm import llm_is_configured
from app.models import (
    MediaItem,
    Project,
    SearchHit,
    SocialComment,
    SocialPost,
)
from app.services.collection.common import canonicalize, result_publication_date
from app.services.collection.media_origin import REDE_SOCIAL
from app.social.comments import (
    iter_comments as _iter_comments,
    normalize_comment as _normalize_comment,
    source_url as _source_url,
)
from app.social.dates import (
    duckduckgo_result_date as _duckduckgo_result_date,
    first as _first,
    parse_datetime as _parse_datetime,
    post_datetime_from_row as _post_datetime_from_row,
    post_datetime_from_url as _post_datetime_from_url,
    relative_duckduckgo_datetime as _relative_duckduckgo_datetime,
    resolve_post_datetime as _resolve_post_datetime,
)
from app.social.sampling import (
    balanced_sample as _balanced_sample,
    mixed_post_comment_order as _mixed_post_comment_order,
)
from app.social.urls import platform_for_url as _platform_for_url, url_key as _url_key
from app.social.analysis import analyze_social_comments as _analyze_social_comments
from app.social.discovery import (
    candidate_urls as _candidate_urls,
    has_duckduckgo_provenance as _has_duckduckgo_provenance,
    social_discovery_terms as _social_discovery_terms,
    social_recovery_terms as _social_recovery_terms,
)
from app.social.methodology import METHODOLOGY_NOTE
from app.social.reporting import social_repercussion_for_report as _social_repercussion_for_report
from app.social.persistence import (
    _append_post_date_provenance,
    _enrich_posts_from_apify_dataset,
    _ensure_post,
    _historical_post,
    _reuse_historical_comments,
    _social_reuse_state,
    _within_window,
)
from app.tools.social import (
    SocialCollectionUnavailable,
    collect_public_comments,
    discover_public_posts,
)



def _discover_and_persist_social_posts(
    db: Session,
    project: Project,
) -> dict[str, Any]:
    settings = get_settings()
    if not getattr(settings, "social_discovery_enabled", True):
        return {
            "queries": 0,
            "returned": 0,
            "eligible_posts": 0,
            "new_items": 0,
            "rounds": 0,
            "terms": [],
            "platforms": {},
        }

    max_terms = max(1, int(settings.social_discovery_queries_per_platform))
    terms = _social_discovery_terms(project)
    query_terms = terms[:max_terms]
    discovered = discover_public_posts(
        terms=query_terms,
        max_queries_per_platform=max_terms,
        results_per_query=settings.social_discovery_results_per_query,
    )
    rounds = 1
    recovery_terms: list[str] = []

    # Se a primeira rodada não devolveu sequer uma URL social, fazemos uma
    # segunda rodada obrigatória com termos mais curtos antes de aceitar NO_POSTS.
    if not any(discovered.values()):
        recovery_terms = _social_recovery_terms(project, query_terms)[:max_terms]
        if recovery_terms:
            recovered = discover_public_posts(
                terms=recovery_terms,
                max_queries_per_platform=max_terms,
                results_per_query=settings.social_discovery_results_per_query,
            )
            rounds = 2
            for platform, rows in recovered.items():
                known = {
                    _url_key(str(row.get("url") or ""))
                    for row in discovered.get(platform, [])
                }
                for row in rows:
                    key = _url_key(str(row.get("url") or ""))
                    if key and key not in known:
                        discovered.setdefault(platform, []).append(row)
                        known.add(key)

    existing = {
        item.canonical_url: item
        for item in db.scalars(
            select(MediaItem).where(MediaItem.project_id == project.id)
        ).all()
        if item.canonical_url
    }
    platform_stats = {
        platform: {"returned": len(rows), "eligible_posts": 0}
        for platform, rows in discovered.items()
    }
    total_terms = len(query_terms) + len(recovery_terms)
    stats = {
        "queries": total_terms * 4,
        "returned": sum(len(rows) for rows in discovered.values()),
        "eligible_posts": 0,
        "new_items": 0,
        "rounds": rounds,
        "terms": [*query_terms, *recovery_terms],
        "primary_terms": query_terms,
        "recovery_terms": recovery_terms,
        "platforms": platform_stats,
    }

    per_platform_limit = max(1, int(settings.apify_social_max_posts_per_platform))
    for platform, rows in discovered.items():
        accepted_for_platform = 0
        for row in rows:
            if accepted_for_platform >= per_platform_limit:
                break
            url = str(row.get("url") or "").strip()
            if _platform_for_url(url) != platform:
                continue

            stats["eligible_posts"] += 1
            platform_stats.setdefault(platform, {"returned": 0, "eligible_posts": 0})
            platform_stats[platform]["eligible_posts"] += 1
            accepted_for_platform += 1
            canonical = canonicalize(url)
            domain = urlparse(url).netloc.lower().split(":", 1)[0]
            title = str(row.get("title") or "Post social").strip() or "Post social"
            snippet = str(row.get("snippet") or "").strip() or None
            published_at, published_at_source, published_at_raw = _duckduckgo_result_date(row)
            query = str(row.get("discovery_query") or "").strip() or None

            item = existing.get(canonical)
            if item is None:
                item = MediaItem(
                    project_id=project.id,
                    title=title,
                    url=url,
                    canonical_url=canonical,
                    domain=domain,
                    published_at=published_at,
                    snippet=snippet,
                    content=snippet or "",
                    source_name=domain,
                    search_source="duckduckgo_social",
                    media_origin=REDE_SOCIAL,
                    source_provenance=[{
                        "source": "duckduckgo_social",
                        "provider": "duckduckgo",
                        "query": query,
                        "platform": platform,
                        "url": url,
                        "published_at": published_at.isoformat() if published_at else None,
                        "published_at_raw": published_at_raw,
                        "date_source": published_at_source,
                        "retrieved_at": row.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
                    }],
                    discovery_purposes=["SOCIAL_DISCOVERY"],
                    status="PENDING",
                    fact_status="PENDING",
                )
                db.add(item)
                db.flush()
                existing[canonical] = item
                stats["new_items"] += 1
            else:
                provenance = list(item.source_provenance or [])
                if not any(
                    isinstance(entry, dict)
                    and entry.get("source") == "duckduckgo_social"
                    and entry.get("query") == query
                    for entry in provenance
                ):
                    provenance.append({
                        "source": "duckduckgo_social",
                        "provider": "duckduckgo",
                        "query": query,
                        "platform": platform,
                        "url": url,
                        "published_at": published_at.isoformat() if published_at else None,
                        "published_at_raw": published_at_raw,
                        "date_source": published_at_source,
                        "retrieved_at": row.get("retrieved_at") or datetime.now(timezone.utc).isoformat(),
                    })
                    item.source_provenance = provenance
                purposes = list(item.discovery_purposes or [])
                if "SOCIAL_DISCOVERY" not in purposes:
                    purposes.append("SOCIAL_DISCOVERY")
                    item.discovery_purposes = purposes
                if not item.media_origin:
                    item.media_origin = REDE_SOCIAL

            db.add(SearchHit(
                project_id=project.id,
                run_id=current_run_id(),
                search_query_id=None,
                media_item_id=item.id,
                provider="duckduckgo_social",
                purpose="SOCIAL_DISCOVERY",
                media_origin=REDE_SOCIAL,
                query=query,
                target=platform,
                title=title,
                url=url,
                canonical_url=canonical,
                domain=domain,
                published_at=published_at,
                published_at_raw=published_at_raw,
                snippet=snippet,
                content=snippet,
                source_name=domain,
                technical_status="COLLECTED",
                technical_flags=[],
                raw_payload={
                    "provider": "duckduckgo_social",
                    "platform": platform,
                    "query": query,
                    "published_at_raw": published_at_raw,
                    "published_at_source": published_at_source,
                    "retrieved_at": row.get("retrieved_at"),
                },
                retrieved_at=datetime.now(timezone.utc),
            ))

    db.commit()
    return stats


def social_repercussion_for_report(db: Session, project_id: int) -> dict:
    return _social_repercussion_for_report(db, project_id)


def analyze_social_comments(db: Session, project: Project) -> dict:
    return _analyze_social_comments(
        db,
        project,
        settings=get_settings(),
        llm_configured=llm_is_configured(),
        agent_factory=get_report_agent,
        report_builder=social_repercussion_for_report,
    )


def collect_social_repercussion(db: Session, project: Project) -> dict:
    settings = get_settings()

    discovery = _discover_and_persist_social_posts(db, project)
    candidates = _candidate_urls(db, project.id)
    discovered_posts = sum(len(rows) for rows in candidates.values())

    if not settings.apify_social_enabled:
        return {
            "status": "DISCOVERED_ONLY" if discovered_posts else "DISABLED",
            "reason": "Apify desativado; descoberta social via DuckDuckGo preservada.",
            "posts": discovered_posts,
            "comments": 0,
            "platforms": {platform: len(rows) for platform, rows in candidates.items()},
            "discovery": discovery,
            "methodology_note": METHODOLOGY_NOTE,
        }
    if not settings.apify_api_token:
        return {
            "status": "DISCOVERED_ONLY" if discovered_posts else "NOT_CONFIGURED",
            "reason": "APIFY_API_TOKEN nao configurado; posts publicos ainda foram descobertos via DuckDuckGo.",
            "posts": discovered_posts,
            "comments": 0,
            "platforms": {platform: len(rows) for platform, rows in candidates.items()},
            "discovery": discovery,
            "methodology_note": METHODOLOGY_NOTE,
        }

    if not candidates:
        return {
            "status": "NO_POSTS",
            "reason": (
                "A busca social dedicada no DuckDuckGo nao localizou posts "
                "publicos elegiveis na amostra"
            ),
            "posts": 0,
            "comments": 0,
            "platforms": {},
            "discovery": discovery,
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
    reused_comments = 0
    reused_posts = 0
    refreshed_posts = 0
    actor_runs = 0

    for platform, rows in candidates.items():
        pending = []
        for url, media_item_id, title in rows:
            post = _ensure_post(
                db, project=project, platform=platform, url=url,
                media_item_id=media_item_id, post_text=title, actor_id=None,
            )
            state, copied = _social_reuse_state(
                db, project, platform, url, post, existing_ids
            )
            reused_comments += copied
            if state == "REUSE":
                reused_posts += 1
                continue
            if state == "REFRESH":
                refreshed_posts += 1
            pending.append((url, media_item_id, title))

        db.flush()
        if not pending:
            platform_result[platform] = {
                "status": "REUSED", "posts": len(rows), "comments": 0
            }
            continue
        rows = pending
        urls = [row[0] for row in rows]

        try:
            actor_id, dataset = collect_public_comments(
                platform=platform,
                urls=urls,
                comments_per_post=int(settings.apify_social_comments_per_post),
            )
            actor_runs += 1
        except SocialCollectionUnavailable as exc:
            platform_result[platform] = {
                "status": "NOT_CONFIGURED"
                if "configurado" in str(exc).lower()
                else "ERROR",
                "posts": len(rows),
                "comments": 0,
                "error": str(exc)[:800],
            }
            continue

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

        metadata_dates_updated = _enrich_posts_from_apify_dataset(
            db,
            platform=platform,
            dataset=dataset,
            post_by_url=post_by_url,
        )

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
            "post_dates_updated": metadata_dates_updated,
        }

    db.commit()
    report = analyze_social_comments(db, project)
    report["discovery"] = discovery
    report["collection"] = {
        "actor_runs": actor_runs,
        "new_comments": new_comments,
        "reused_comments": reused_comments,
        "reused_posts": reused_posts,
        "refreshed_posts": refreshed_posts,
        "platforms": platform_result,
    }
    return report

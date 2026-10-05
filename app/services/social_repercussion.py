from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import cost_context, current_run_id
from app.llm import llm_is_configured
from app.models import (
    MediaItem,
    Project,
    SearchHit,
    SocialAnalysis,
    SocialComment,
    SocialPost,
)
from app.schemas import SocialCommentBatchResponse, SocialDiscourseAnalysisResponse
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
from app.social.methodology import METHODOLOGY_NOTE
from app.social.reporting import social_repercussion_for_report as _social_repercussion_for_report
from app.tools.social import (
    SocialCollectionUnavailable,
    collect_public_comments,
    discover_public_posts,
)



def _has_duckduckgo_provenance(item: MediaItem) -> bool:
    source = str(item.search_source or "").strip().lower()
    if source.startswith("duckduckgo"):
        return True
    for entry in item.source_provenance or []:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("source") or entry.get("provider") or "").strip().lower()
        if provider.startswith("duckduckgo"):
            return True
    return False


def _candidate_urls(
    db: Session,
    project_id: int,
) -> dict[str, list[tuple[str, int | None, str | None]]]:
    """Posts sociais conhecidos com descoberta DuckDuckGo auditavel."""
    found: dict[str, dict[str, tuple[str, int | None, str | None]]] = defaultdict(dict)

    for item in db.scalars(
        select(MediaItem).where(MediaItem.project_id == project_id)
    ).all():
        if not _has_duckduckgo_provenance(item):
            continue
        platform = _platform_for_url(item.url)
        if not platform:
            continue
        found[platform][_url_key(item.url)] = (item.url, item.id, item.title)

    for hit in db.scalars(
        select(SearchHit).where(SearchHit.project_id == project_id)
    ).all():
        if not str(hit.provider or "").strip().lower().startswith("duckduckgo"):
            continue
        platform = _platform_for_url(hit.url)
        if not platform:
            continue
        url = str(hit.url or "").strip()
        found[platform].setdefault(
            _url_key(url), (url, hit.media_item_id, hit.title)
        )

    limit = max(1, int(get_settings().apify_social_max_posts_per_platform))
    return {
        platform: list(rows.values())[:limit]
        for platform, rows in found.items()
        if rows
    }


def _social_discovery_terms(project: Project) -> list[str]:
    """Gera consultas sociais curtas, distintas da pergunta literal do relatório."""
    profile = project.topic_profile or {}
    values: list[str] = []

    # Prioriza formulações que já foram normalizadas pelo perfil/planejador.
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
    # Combina atores quando isso produz uma busca social natural, como
    # "Lula Bolsonaro", sem depender da pergunta longa do usuário.
    if len(actors) >= 2:
        values.append(" ".join(actors[:2]))
    values.extend(actors)

    # A pergunta literal fica por último, apenas como fallback auditável.
    values.append(project.topic)

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" ?.,;:")
        if len(text) < 3:
            continue

        # Remove introduções interrogativas que não aparecem naturalmente em posts.
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

        # Evita consultas excessivamente longas; rede social responde melhor
        # a núcleos temáticos compactos.
        words = text.split()
        if len(words) > 8:
            text = " ".join(words[:8])

        key = text.casefold()
        if len(text) < 3 or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned


def _social_recovery_terms(project: Project, primary_terms: list[str]) -> list[str]:
    """Segunda rodada mais ampla quando a descoberta inicial retorna zero."""
    profile = project.topic_profile or {}
    values: list[str] = []

    for key in ("subject_terms", "actions", "actors", "organizations"):
        raw = profile.get(key)
        if isinstance(raw, list):
            values.extend(str(value) for value in raw if str(value).strip())

    # Acrescenta âncoras curtas derivadas dos termos iniciais.
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
        if location and location.casefold() not in text.casefold() and len(text.split()) <= 3:
            text = f"{text} {location}".strip()
        if year and year not in text and len(text.split()) <= 4:
            text = f"{text} {year}".strip()
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned


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

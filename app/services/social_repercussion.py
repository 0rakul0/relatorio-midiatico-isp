from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

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
from app.tools.social import (
    SocialCollectionUnavailable,
    collect_public_comments,
    discover_public_posts,
)


METHODOLOGY_NOTE = (
    "A camada social descreve apenas comentarios publicamente visiveis nos "
    "posts localizados na amostra. Ela nao e pesquisa amostral da populacao "
    "e nao deve ser interpretada como opiniao publica do Estado do Rio de Janeiro."
)



def _parse_reference_datetime(value: object) -> datetime:
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


def _relative_duckduckgo_datetime(
    value: object,
    *,
    retrieved_at: object,
) -> datetime | None:
    """Resolve datas relativas do DDG usando o instante real da coleta."""
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return None
    reference = _parse_reference_datetime(retrieved_at)

    fixed = {
        "today": timedelta(0),
        "hoje": timedelta(0),
        "yesterday": timedelta(days=1),
        "ontem": timedelta(days=1),
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
        if not match:
            continue
        amount = int(match.group(1))
        delta = timedelta(**{unit: amount})
        return (reference - delta).replace(tzinfo=None)
    return None


def _duckduckgo_result_date(row: dict) -> tuple[date | None, str | None, str | None]:
    """Retorna data, proveniência e valor bruto preservando auditabilidade."""
    raw = row.get("published_at_raw")
    if raw in (None, ""):
        raw = row.get("published_at")
    raw_text = str(raw).strip() if raw not in (None, "") else None

    explicit = result_publication_date(raw)
    if explicit is not None:
        return explicit, "duckduckgo_metadata", raw_text

    relative = _relative_duckduckgo_datetime(
        raw,
        retrieved_at=row.get("retrieved_at"),
    )
    if relative is not None:
        return relative.date(), "duckduckgo_relative_date", raw_text
    return None, None, raw_text


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

    if host.endswith("tiktok.com") and "/video/" in path:
        return "tiktok"

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
        timestamp = float(text)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
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



def _post_datetime_from_row(platform: str, row: dict) -> tuple[datetime | None, str | None]:
    """Extrai somente timestamps que descrevem o POST de origem.

    Não reutiliza o timestamp genérico do comentário, pois isso faria a data
    do comentário parecer data do post no anexo.
    """
    keys = (
        "postCreatedAt",
        "post_created_at",
        "postTimestamp",
        "post_timestamp",
        "postDate",
        "post_date",
        "publicationDate",
        "publication_date",
        "takenAt",
        "taken_at",
        "post.createdAt",
        "post.created_at",
        "post.timestamp",
        "post.date",
        "post.takenAt",
        "post.taken_at",
        "metadata.sourceTweetCreatedAt",
        "metadata.source_tweet_created_at",
        "metadata.sourceTweet.created_at",
        "sourceTweet.createdAt",
        "sourceTweet.created_at",
        "tweet.createdAt",
        "tweet.created_at",
    )
    value = _first(row, keys)
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed, "apify_post_metadata"
    return None, None


def _post_datetime_from_url(platform: str, url: str) -> tuple[datetime | None, str | None]:
    """Deriva data apenas de IDs cuja codificação temporal é conhecida."""
    raw = str(url or "")
    try:
        path = urlparse(raw).path
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
            video_id = int(match.group(1))
            timestamp = video_id >> 32
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc).replace(tzinfo=None)
            if 2016 <= parsed.year <= datetime.now(timezone.utc).year + 1:
                return parsed, "tiktok_video_id"
    except (OverflowError, OSError, ValueError):
        return None, None

    return None, None


def _resolve_post_datetime(
    platform: str,
    *,
    row: dict | None = None,
    url: str = "",
    media_published_at: date | None = None,
) -> tuple[datetime | None, str | None]:
    if row:
        parsed, source = _post_datetime_from_row(platform, row)
        if parsed is not None:
            return parsed, source
    if media_published_at is not None:
        return datetime.combine(media_published_at, datetime.min.time()), "duckduckgo_metadata"
    return _post_datetime_from_url(platform, url)


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
            "videoWebUrl",
            "webVideoUrl",
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
            "replyId",
            "cid",
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
            "replyText",
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
                "createTimeISO",
                "create_time_iso",
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
                        "repliesToId",
                        "replies_to_id",
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
                    "diggCount",
                    "digg_count",
                    "favouriteCount",
                    "favoriteCount",
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
                    "replyCommentTotal",
                    "reply_comment_total",
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
        analysis.discourse_analysis = {}
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
        analysis.discourse_analysis = {}
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

    discourse_analysis: dict = {}
    if analyzed_indices:
        # Segunda leitura: sai da simples classificação e interpreta o debate
        # observado na amostra, mantendo os comentários anonimizados e sem
        # generalizar os achados para a população.
        discourse_comments = [
            {
                "platform": comment.platform,
                "text": comment.text[:900],
                "like_count": comment.like_count,
                "reply_count": comment.reply_count,
            }
            for comment in sample[: min(len(sample), 80)]
        ]
        discourse_payload = {
            "project": {
                "topic": project.topic,
                "collection_start": project.collection_start.isoformat(),
                "collection_end": project.collection_end.isoformat(),
            },
            "sample": {
                "comments_collected": len(comments),
                "comments_classified": len(analyzed_indices),
                "platform_counts": platform_counts,
                "sentiment_counts": dict(sentiment),
                "emotion_counts": dict(emotion),
                "position_counts": dict(position),
                "themes": [
                    {"theme": theme, "count": count}
                    for theme, count in themes.most_common(12)
                ],
            },
            "comments": discourse_comments,
            "methodology": METHODOLOGY_NOTE,
        }
        discourse_analysis = get_report_agent().run(
            task="social_discourse_analysis",
            payload=discourse_payload,
            extra_instructions=(
                "Faça uma análise qualitativa e discursiva dos comentários públicos da amostra. "
                "Não se limite a repetir percentuais de positivo/negativo. Explique narrativas, "
                "argumentos, conflitos, formas de interação, rejeição, apoio, fadiga, confiança, "
                "desconfiança, ironia, personalismo e sinais de polarização quando sustentados pelos comentários. "
                "Nunca escreva 'a população pensa', 'os brasileiros são' ou equivalentes. Use formulações como "
                "'entre os comentários analisados', 'uma parcela da amostra manifesta' e 'o debate observado sugere'. "
                "Não invente grupos, intenções ou causas que não estejam sustentados pela amostra. "
                "Não reproduza nomes de usuários nem dados pessoais. Aponte contradições e limitações da amostra."
            ),
            response_model=SocialDiscourseAnalysisResponse,
            schema_name="social_discourse_analysis_v1",
            max_output_tokens=5000,
        )

    analysis.status = "COMPLETED" if analyzed_indices else "COLLECTED_ONLY"
    analysis.analyzed_comments = len(analyzed_indices)
    analysis.sentiment_counts = dict(sentiment)
    analysis.emotion_counts = dict(emotion)
    analysis.position_counts = dict(position)
    analysis.themes = [
        {"theme": theme, "count": count}
        for theme, count in themes.most_common(12)
    ]
    analysis.discourse_analysis = discourse_analysis
    analysis.summary = (
        discourse_analysis.get("overall_reading")
        if discourse_analysis
        else _human_summary(
        len(analyzed_indices),
        sentiment,
        emotion,
        position,
        themes,
    )
    )
    db.commit()
    return social_repercussion_for_report(db, project.id)


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
            published_at, date_source = _resolve_post_datetime(
                post.platform,
                url=post.url,
                media_published_at=(
                    media.published_at
                    if media is not None
                    else None
                ),
            )
        else:
            date_source = "social_post"
        published_label = published_at.date().isoformat() if published_at is not None else None
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

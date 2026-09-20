"""Reusable global news corpus and deterministic candidate reuse."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CorpusDocument, MediaItem, Project, ProjectCorpusLink
from app.services.collection.media_origin import classify_media_origin
from app.topic_profile import normalized_text
from app.year_utils import find_years

_STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma", "ao",
    "aos", "que", "sobre", "rj", "rio", "janeiro",
}


def _tokens(value: str | None) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", normalized_text(value or ""))
        if len(token) > 2 and token not in _STOPWORDS
    }


def _compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _profile_terms(project: Project) -> tuple[set[str], list[str]]:
    profile = project.topic_profile or {}
    values = [project.topic]
    phrases: list[str] = []
    for key in ("product_name", "product_anchor", "event_anchor"):
        value = _compact(profile.get(key))
        if value:
            values.append(value)
            phrases.append(value)
    for key in (
        "product_search_variants", "event_search_variants",
        "fact_discovery_variants", "subject_terms", "actors", "actions",
        "locations", "organizations", "search_synonyms",
    ):
        values.extend(_compact(value) for value in (profile.get(key) or []) if _compact(value))
    tokens: set[str] = set()
    for value in values:
        tokens.update(_tokens(value))
    return tokens, phrases


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _project_years(project: Project) -> set[str]:
    profile = project.topic_profile or {}
    years: set[str] = set()
    for value in [
        project.topic,
        _compact(profile.get("product_name")),
        _compact(profile.get("product_anchor")),
        _compact(profile.get("event_anchor")),
    ]:
        years.update(find_years(value or ""))
    if project.event_start:
        years.add(str(project.event_start.year))
    if project.event_end:
        years.add(str(project.event_end.year))
    return years


def project_similarity(current: Project, previous: Project) -> float:
    current_topic = normalized_text(current.topic or "")
    previous_topic = normalized_text(previous.topic or "")
    if current_topic and current_topic == previous_topic:
        return 1.0

    current_profile = current.topic_profile or {}
    previous_profile = previous.topic_profile or {}
    current_product = normalized_text(_compact(
        current_profile.get("product_anchor") or current_profile.get("product_name")
    ))
    previous_product = normalized_text(_compact(
        previous_profile.get("product_anchor") or previous_profile.get("product_name")
    ))
    if current_product and previous_product and current_product == previous_product:
        current_years = _project_years(current)
        previous_years = _project_years(previous)
        return 0.48 if current_years and previous_years and current_years.isdisjoint(previous_years) else 0.95

    current_event = normalized_text(_compact(current_profile.get("event_anchor")))
    previous_event = normalized_text(_compact(previous_profile.get("event_anchor")))
    bonus = 0.0
    if current_event and previous_event:
        bonus = 0.55 if current_event == previous_event else (
            0.35 * _jaccard(_tokens(current_event), _tokens(previous_event))
        )

    current_terms, _ = _profile_terms(current)
    previous_terms, _ = _profile_terms(previous)
    lexical = _jaccard(current_terms, previous_terms)
    score = min(1.0, bonus + (0.65 * lexical))

    current_locations = {
        normalized_text(str(value))
        for value in (current_profile.get("locations") or [])
        if str(value).strip()
    }
    previous_locations = {
        normalized_text(str(value))
        for value in (previous_profile.get("locations") or [])
        if str(value).strip()
    }
    shared_distinctive = {
        token for token in current_terms.intersection(previous_terms)
        if len(token) >= 5
    }
    if shared_distinctive and current_locations.intersection(previous_locations):
        score = max(score, 0.38)

    current_years = _project_years(current)
    previous_years = _project_years(previous)
    if current_years and previous_years and current_years.isdisjoint(previous_years):
        score *= 0.72
    return score


def document_similarity(project: Project, document: CorpusDocument) -> float:
    project_terms, phrases = _profile_terms(project)
    body = " ".join(filter(None, [
        document.title, document.snippet, (document.content or "")[:4000]
    ]))
    body_norm = normalized_text(body)
    lexical = _jaccard(project_terms, _tokens(body))
    anchor = 0.0
    for phrase in phrases:
        phrase_norm = normalized_text(phrase)
        if phrase_norm and phrase_norm in body_norm:
            anchor = max(anchor, 0.72)
    topic_norm = normalized_text(project.topic or "")
    if topic_norm and topic_norm in body_norm:
        anchor = max(anchor, 0.82)
    return min(1.0, max(anchor, min(0.70, lexical * 1.55)))


def _merge_provenance(existing: list | None, incoming: list | None) -> list:
    rows = list(existing or [])
    seen = {
        (
            str(row.get("source") or ""), str(row.get("url") or ""),
            str(row.get("query") or ""), str(row.get("retrieved_at") or ""),
        )
        for row in rows if isinstance(row, dict)
    }
    for row in incoming or []:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("source") or ""), str(row.get("url") or ""),
            str(row.get("query") or ""), str(row.get("retrieved_at") or ""),
        )
        if key not in seen:
            rows.append(dict(row))
            seen.add(key)
    return rows


def sync_media_item_to_corpus(
    db: Session,
    item: MediaItem,
    *,
    origin: str | None = None,
    source_project_id: int | None = None,
) -> CorpusDocument:
    canonical = _compact(item.canonical_url or item.url)
    document = db.scalar(
        select(CorpusDocument).where(CorpusDocument.canonical_url == canonical)
    )
    now = datetime.now(timezone.utc)

    if document is None:
        document = CorpusDocument(
            canonical_url=canonical,
            url=item.url,
            title=item.title,
            domain=item.domain,
            published_at=item.published_at,
            snippet=item.snippet,
            content=item.content,
            source_name=item.source_name,
            view_count=item.view_count,
            search_source=item.search_source,
            media_origin=item.media_origin
            or classify_media_origin(item.url, item.domain),
            source_provenance=list(item.source_provenance or []),
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(document)
        db.flush()
    else:
        document.last_seen_at = now
        if item.url:
            document.url = item.url
        if item.title and (not document.title or document.title == "Sem titulo"):
            document.title = item.title
        if not document.domain and item.domain:
            document.domain = item.domain
        if not document.published_at and item.published_at:
            document.published_at = item.published_at
        if item.snippet and len(item.snippet) > len(document.snippet or ""):
            document.snippet = item.snippet
        if item.content and len(item.content) > len(document.content or ""):
            document.content = item.content
        if not document.source_name and item.source_name:
            document.source_name = item.source_name
        if not document.media_origin and item.media_origin:
            document.media_origin = item.media_origin
        if not document.media_origin:
            document.media_origin = classify_media_origin(item.url, item.domain)
        if item.view_count is not None:
            document.view_count = item.view_count
        document.source_provenance = _merge_provenance(
            document.source_provenance, item.source_provenance
        )

    item.corpus_document_id = document.id
    item.corpus_origin = origin or item.corpus_origin or "SEARCH"

    link = db.scalar(
        select(ProjectCorpusLink).where(
            ProjectCorpusLink.project_id == item.project_id,
            ProjectCorpusLink.document_id == document.id,
        )
    )
    if link is None:
        db.add(ProjectCorpusLink(
            project_id=item.project_id,
            document_id=document.id,
            media_item_id=item.id,
            origin=origin or item.corpus_origin or "SEARCH",
            source_project_id=source_project_id,
            relation_status=item.status or "PENDING",
            relation_type=item.relation_type,
            relevance_evidence=item.relevance_evidence,
            created_at=now,
            updated_at=now,
        ))
    else:
        link.media_item_id = item.id
        link.updated_at = now
        link.relation_status = item.status or link.relation_status
        link.relation_type = item.relation_type or link.relation_type
        link.relevance_evidence = item.relevance_evidence or link.relevance_evidence
        if origin:
            link.origin = origin
        if source_project_id is not None:
            link.source_project_id = source_project_id
    return document


def backfill_global_corpus(db: Session) -> int:
    settings = get_settings()
    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.corpus_document_id.is_(None))
        .order_by(MediaItem.id.asc())
        .limit(settings.corpus_backfill_limit)
    ).all()
    for item in items:
        sync_media_item_to_corpus(
            db, item, origin=item.corpus_origin or "HISTORICAL"
        )
    db.flush()
    return len(items)


def _in_requested_window(project: Project, document: CorpusDocument) -> bool:
    if not project.has_custom_date_window or document.published_at is None:
        return True
    return project.collection_start <= document.published_at <= project.collection_end


def _document_reference_date(document: CorpusDocument) -> date | None:
    """Data que define a idade do documento: publicacao, ou primeira coleta."""
    if document.published_at:
        return document.published_at
    first_seen = getattr(document, "first_seen_at", None)
    if isinstance(first_seen, datetime):
        return first_seen.date()
    if isinstance(first_seen, date):
        return first_seen
    return None


def _document_expired(
    document: CorpusDocument,
    project: Project,
    today: date,
    max_age_days: int,
) -> bool:
    """True quando o documento passou da validade para reuso.

    Documentos dentro de uma janela de datas explicitamente pedida nunca
    expiram: o usuario pediu aquele periodo. Sem data determinavel, o
    documento e considerado novo (ausencia de evidencia nao e evidencia
    de antiguidade).
    """
    if not max_age_days:
        return False
    if (
        project.has_custom_date_window
        and document.published_at is not None
        and project.collection_start <= document.published_at <= project.collection_end
    ):
        return False
    ref = _document_reference_date(document)
    if ref is None:
        return False
    return (today - ref).days > max_age_days


def reuse_prior_corpus(
    db: Session,
    project: Project,
    *,
    progress_detail=None,
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.enable_corpus_reuse:
        return {
            "enabled": False, "reused": 0, "candidate_documents": 0,
            "source_projects": 0, "exact_topic_match": False,
            "covered_domains": [], "coverage_start": None, "coverage_end": None,
            "expired_documents": 0,
        }

    indexed = backfill_global_corpus(db)
    if progress_detail:
        progress_detail(f"Corpus historico indexado: {indexed} item(ns) novo(s)")

    previous_projects = db.scalars(
        select(Project)
        .where(Project.id != project.id)
        .order_by(Project.id.desc())
        .limit(settings.corpus_reuse_project_limit)
    ).all()
    related: list[tuple[Project, float]] = []
    for previous in previous_projects:
        score = project_similarity(project, previous)
        if score >= settings.corpus_reuse_min_project_score:
            related.append((previous, score))
    related.sort(key=lambda row: row[1], reverse=True)

    existing = set(db.scalars(
        select(MediaItem.canonical_url).where(MediaItem.project_id == project.id)
    ).all())
    best: dict[int, tuple[float, Project, CorpusDocument]] = {}

    for previous, project_score in related:
        rows = db.execute(
            select(ProjectCorpusLink, CorpusDocument)
            .join(CorpusDocument, CorpusDocument.id == ProjectCorpusLink.document_id)
            .where(ProjectCorpusLink.project_id == previous.id)
        ).all()
        for _link, document in rows:
            if not _in_requested_window(project, document):
                continue
            document_score = document_similarity(project, document)
            combined = (0.58 * project_score) + (0.42 * document_score)
            if combined < settings.corpus_reuse_min_document_score:
                continue
            current = best.get(document.id)
            if current is None or combined > current[0]:
                best[document.id] = (combined, previous, document)

    ranked = sorted(best.values(), key=lambda row: row[0], reverse=True)
    ranked = ranked[: settings.corpus_reuse_max_candidates]

    reused = 0
    expired = 0
    today = date.today()
    max_age_days = max(0, int(settings.corpus_reuse_max_age_days or 0))
    domains: set[str] = set()
    dates = []
    source_project_ids: set[int] = set()
    exact_topic_match = False

    for _score, previous, document in ranked:
        if document.canonical_url in existing:
            continue
        if _document_expired(document, project, today, max_age_days):
            expired += 1
            continue
        source_project_ids.add(previous.id)
        exact_topic_match = exact_topic_match or (
            normalized_text(previous.topic or "") == normalized_text(project.topic or "")
        )
        if document.domain:
            domains.add(document.domain.lower())
        if document.published_at:
            dates.append(document.published_at)

        provenance = _merge_provenance(document.source_provenance, [{
            "source": "corpus_reuse",
            "title": document.title,
            "url": document.url,
            "published_at": document.published_at.isoformat() if document.published_at else None,
            "snippet": document.snippet,
            "channel": document.source_name,
            "query": None,
            "target": None,
            "reused_from_project_id": previous.id,
            "reused_from_topic": previous.topic,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }])

        item = MediaItem(
            project_id=project.id,
            query_id=None,
            corpus_document_id=document.id,
            corpus_origin="REUSED",
            title=document.title or "Sem titulo",
            url=document.url or document.canonical_url,
            canonical_url=document.canonical_url,
            domain=document.domain,
            published_at=document.published_at,
            snippet=document.snippet,
            content=document.content or document.snippet or "",
            source_name=document.source_name,
            view_count=document.view_count,
            search_source="corpus_reuse",
            media_origin=document.media_origin
            or classify_media_origin(document.url, document.domain),
            source_provenance=provenance,
            discovery_purposes=["MEDIA_REPERCUSSION", "CORPUS_REUSE"],
            status="PENDING",
            fact_status="PENDING",
        )
        db.add(item)
        db.flush()
        sync_media_item_to_corpus(
            db, item, origin="REUSED", source_project_id=previous.id
        )
        existing.add(document.canonical_url)
        reused += 1

    summary = {
        "enabled": True,
        "reused": reused,
        "expired_documents": expired,
        "candidate_documents": len(ranked),
        "source_projects": len(source_project_ids),
        "source_project_ids": sorted(source_project_ids),
        "exact_topic_match": exact_topic_match,
        "covered_domains": sorted(domains),
        "coverage_start": min(dates).isoformat() if dates else None,
        "coverage_end": max(dates).isoformat() if dates else None,
    }
    profile = dict(project.topic_profile or {})
    profile["corpus_reuse"] = summary
    project.topic_profile = profile
    db.commit()
    return summary

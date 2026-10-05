from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.database import get_db
from app.fact_layer import fact_events_for_report
from app.models import AcademicPaper, Classification, MediaItem, SearchQuery
from app.services.collection.media_origin import classify_media_origin


router = APIRouter(tags=["project-preview"])


@router.get("/projects/{project_id}/research-plan")
def project_research_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Expõe a interpretação do tema e a estratégia de busca sem nova chamada à LLM."""
    project = project_or_404(db, user, project_id)
    profile = project.topic_profile or {}
    options = project.execution_options or {}
    execution_plan = project.execution_plan or {}

    queries = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project_id)
        .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
    ).all()

    project_type_labels = {
        "INSTITUTIONAL_PRODUCT": "Produto institucional",
        "EVENT_TOPIC": "Tema factual / evento",
        "GENERAL_TOPIC": "Tema geral",
        "AUTO": "Classificação automática",
    }

    primary_anchor = (
        profile.get("product_name")
        or profile.get("event_anchor")
        or profile.get("product_anchor")
        or project.topic
    )

    variants = []
    for key in (
        "product_search_variants",
        "event_search_variants",
        "fact_discovery_variants",
        "search_synonyms",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            variants.extend(str(item).strip() for item in value if str(item).strip())
    variants = list(dict.fromkeys(variants))

    entities = {
        "atores": list(profile.get("actors") or []),
        "ações": list(profile.get("actions") or []),
        "locais": list(profile.get("locations") or []),
        "organizações": list(profile.get("organizations") or []),
        "assuntos": list(profile.get("subject_terms") or []),
    }

    return {
        "project_id": project.id,
        "original_topic": project.topic,
        "interpreted_topic": primary_anchor,
        "project_type": project.project_type,
        "project_type_label": project_type_labels.get(project.project_type, project.project_type),
        "temporal_mode": options.get("temporal_mode"),
        "collection_window": {
            "start": project.collection_start.isoformat() if project.has_custom_date_window and project.collection_start else None,
            "end": project.collection_end.isoformat() if project.has_custom_date_window and project.collection_end else None,
            "source": options.get("collection_window_source"),
        },
        "event_window": {
            "start": project.event_start.isoformat() if project.event_start else None,
            "end": project.event_end.isoformat() if project.event_end else None,
            "source": options.get("event_window_source"),
        },
        "geographic_scopes": list(options.get("geographic_scopes") or []),
        "corrections_and_variants": variants,
        "entities": entities,
        "inclusion_rules": list(profile.get("inclusion_rules") or []),
        "exclusion_rules": list(profile.get("exclusion_rules") or []),
        "search_strategy": profile.get("search_strategy") or {},
        "execution_plan": execution_plan,
        "queries": [
            {
                "id": row.id,
                "query": row.query,
                "kind": row.kind,
                "purpose": row.purpose,
                "rationale": row.rationale,
                "priority": row.priority,
                "execution_status": row.execution_status,
                "executed_at": row.executed_at.isoformat() if row.executed_at else None,
                "providers_attempted": list(row.providers_attempted or []),
                "results_returned": row.results_returned,
                "results_accepted": row.results_accepted,
            }
            for row in queries
        ],
    }



@router.get("/projects/{project_id}/execution-preview")
def project_execution_preview(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Prévia auditável das etapas enquanto o relatório ainda está sendo executado."""
    project = project_or_404(db, user, project_id)

    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id == project_id)
        .order_by(MediaItem.id.desc())
    ).all()
    queries = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project_id)
        .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
    ).all()

    def item_origin(item: MediaItem) -> str:
        return item.media_origin or classify_media_origin(item.url, item.domain)

    domains = Counter(
        (item.domain or "").lower().removeprefix("www.")
        for item in items
        if item.domain
    )
    origins = Counter(item_origin(item) for item in items)
    statuses = Counter(str(item.status or "PENDING") for item in items)
    query_statuses = Counter(str(row.execution_status or "PENDING") for row in queries)

    recent_items = [
        {
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "domain": item.domain,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "status": item.status,
            "media_origin": item_origin(item),
            "search_source": item.search_source,
            "corpus_origin": item.corpus_origin,
        }
        for item in items[:24]
    ]

    classifications = db.scalars(
        select(Classification)
        .join(MediaItem, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id)
    ).all()
    themes = Counter(
        str(row.theme)
        for row in classifications
        if row.theme
    )

    fact_rows = fact_events_for_report(db, project_id)
    fact_statuses = Counter(
        str(row.get("resolution_status") or "PENDING")
        for row in fact_rows
    )

    papers = db.scalars(
        select(AcademicPaper)
        .where(AcademicPaper.project_id == project_id)
        .order_by(AcademicPaper.id.desc())
    ).all()

    return {
        "project_id": project.id,
        "collection": {
            "items_found": len(items),
            "new_items": sum(str(item.corpus_origin or "SEARCH").upper() != "REUSED" for item in items),
            "reused_items": sum(str(item.corpus_origin or "").upper() == "REUSED" for item in items),
            "origins": dict(origins),
            "domains": [
                {"domain": domain, "items": count}
                for domain, count in domains.most_common(20)
            ],
            "recent_items": recent_items,
            "queries": {
                "total": len(queries),
                "executed": sum(row.executed_at is not None for row in queries),
                "statuses": dict(query_statuses),
                "returned": sum(int(row.results_returned or 0) for row in queries),
                "accepted": sum(int(row.results_accepted or 0) for row in queries),
            },
        },
        "social": {
            "social_items": int(origins.get("REDE_SOCIAL", 0)),
            "youtube_items": int(origins.get("YOUTUBE", 0)),
        },
        "validation": {
            "status_counts": dict(statuses),
            "valid": int(statuses.get("VALID", 0)),
            "pending": int(statuses.get("PENDING", 0)),
            "discarded": sum(
                count
                for status, count in statuses.items()
                if status not in {"VALID", "PENDING"}
            ),
        },
        "facts": {
            "events": len(fact_rows),
            "statuses": dict(fact_statuses),
        },
        "classification": {
            "classified_items": len(classifications),
            "themes": [
                {"theme": theme, "items": count}
                for theme, count in themes.most_common(12)
            ],
        },
        "academic": {
            "papers": len(papers),
            "recent": [
                {
                    "title": paper.title_ptbr or paper.title,
                    "provider": paper.provider,
                    "url": paper.url,
                }
                for paper in papers[:8]
            ],
        },
    }

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.models import Project, SearchQuery
from app.schemas import GapFillResponse
from app.search.gap_fill import (
    gap_fallback_angles,
    has_site_operator,
    zero_corpus_fallback_angles,
)
from app.search.guards import is_redundant, media_query_is_acceptable
from app.search.persistence import add_query, existing_queries
from app.source_registry import PRIORITY_MEDIA_SOURCES


GAP_FILLABLE_RESULTS = {
    "sem item validado na amostra",
    "não consultado individualmente nesta execução",
}


def detect_coverage_gaps(db: Session, project: Project) -> dict[str, Any]:
    from app.services.metrics import metrics as project_metrics

    settings = get_settings()
    data = project_metrics(db, project.id)
    checks = {
        str(item.get("portal")): str(item.get("result") or "")
        for item in (data.get("portal_checks") or [])
    }
    uncovered = [
        {"portal": label, "domain": domain, "result": checks.get(label, "")}
        for label, domain in PRIORITY_MEDIA_SOURCES
        if checks.get(label, "") in GAP_FILLABLE_RESULTS
    ]
    valid_items = int(data.get("valid_items") or 0)
    zero_corpus = valid_items == 0
    return {
        "uncovered_portals": uncovered,
        "valid_items": valid_items,
        "target_items": int(settings.target_media_items),
        "zero_corpus": zero_corpus,
        "needs_fill": bool(uncovered) or zero_corpus,
    }


def plan_gap_fill_queries(
    db: Session,
    project: Project,
    gaps: dict[str, Any],
    *,
    max_queries: int | None = None,
) -> list[SearchQuery]:
    settings = get_settings()
    configured_cap = max(
        0,
        int(
            settings.max_gap_fill_queries
            if max_queries is None
            else max_queries
        ),
    )
    uncovered = list(gaps.get("uncovered_portals") or [])
    zero_corpus = bool(gaps.get("zero_corpus"))
    cap = max(1, configured_cap) if zero_corpus else configured_cap
    if cap <= 0 or (not uncovered and not zero_corpus):
        return []

    executed = [
        row.query
        for row in db.scalars(
            select(SearchQuery).where(SearchQuery.project_id == project.id)
        ).all()
    ]
    existing = existing_queries(db, project.id)
    selected = list(executed)
    candidates: list[tuple[str, str]] = []

    if llm_is_configured():
        try:
            result = get_report_agent().run(
                task="gap_planner",
                payload={
                    "topic": project.topic,
                    "project_type": project.project_type,
                    "topic_profile": project.topic_profile,
                    "uncovered_portals": uncovered,
                    "zero_corpus": zero_corpus,
                    "zero_corpus_recovery": (
                        "A primeira validação terminou com zero itens. Gere consultas realmente novas: "
                        "corrija/varie grafias plausíveis de entidades, use vocabulário jornalístico e "
                        "inclua ao menos uma consulta mais ampla, preservando local/objeto."
                        if zero_corpus
                        else None
                    ),
                    "executed_queries": executed,
                    "max_queries": cap,
                },
                schema_name="gap_fill_v1",
                response_model=GapFillResponse,
                max_output_tokens=2500,
            )
        except RuntimeError:
            result = {"queries": []}

        for item in (result.get("queries") or [])[:cap]:
            query = " ".join(str(item.get("query") or "").split()).strip()
            if not query or has_site_operator(query):
                continue
            if not media_query_is_acceptable(project, query):
                continue
            if query in existing or is_redundant(query, selected):
                continue
            candidates.append((query, str(item.get("rationale") or "")))
            selected.append(query)

    if zero_corpus and len(candidates) < cap:
        for query, rationale in zero_corpus_fallback_angles(
            project,
            selected,
            cap - len(candidates),
        ):
            if query in existing:
                continue
            candidates.append((query, rationale))
            selected.append(query)

    if len(candidates) < cap:
        for query, rationale in gap_fallback_angles(
            project,
            selected,
            cap - len(candidates),
        ):
            if query in existing:
                continue
            candidates.append((query, rationale))
            selected.append(query)

    created: list[SearchQuery] = []
    for query, rationale in candidates[:cap]:
        row = add_query(
            db,
            project,
            existing,
            query=query,
            kind="media_zero_recovery"
            if zero_corpus
            else "media_complementary",
            purpose="MEDIA_REPERCUSSION",
            rationale=(
                f"[zero-corpus recovery] {rationale}"
                if zero_corpus
                else f"[cobertura complementar] {rationale}"
            )[:1000],
            priority=3,
        )
        if row:
            created.append(row)
    db.commit()
    return created

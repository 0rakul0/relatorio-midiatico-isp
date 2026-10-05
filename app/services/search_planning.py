from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.media_scout import MediaScout, ScoutTask
from app.models import OfficialFact, Project, SearchQuery
from app.schemas import GapFillResponse, ReportPlanResponse
from app.source_registry import (
    OFFICIAL_OPERATION_INVENTORY_SOURCES,
    OFFICIAL_SECURITY_SOURCES,
    PRIORITY_MEDIA_SOURCES,
)
from app.topic_profile import build_topic_profile, canonicalize_known_locations, normalized_text
from app.services.collection.guards import query_preserves_project_anchor
from app.services.execution_profile import (
    execution_flags,
    heuristic_execution_plan,
    sanitize_execution_plan,
)
from app.services.project_profile import project_payload
from app.search.inventory import (
    PT_MONTHS as _PT_MONTHS,
    annual_event_inventory_queries,
    official_operation_inventory_queries,
)
from app.search.guards import (
    is_redundant as _is_redundant,
    media_query_is_acceptable as _media_query_is_acceptable,
    query_is_acceptable as _query_is_acceptable,
    query_tokens as _query_tokens,
    semantic_similarity as _semantic_similarity,
)
from app.search.gap_fill import (
    gap_fallback_angles as _gap_fallback_angles,
    has_site_operator as _has_site_operator,
    zero_corpus_fallback_angles as _zero_corpus_fallback_angles,
)
from app.search.persistence import (
    add_query as _add_query,
    existing_queries as _existing_queries,
)
from app.search.gap_planning import (
    detect_coverage_gaps,
    plan_gap_fill_queries,
)
from app.search.strategy import _persist_strategy_queries, _sanitize_strategy
from app.search.refinement import plan_missing_month_operation_inventory_queries

_INITIAL_PURPOSES = {"MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"}


def _annual_event_inventory_queries(project: Project) -> list[tuple[str, str]]:
    return annual_event_inventory_queries(project, settings=get_settings())


def _official_operation_inventory_queries(project: Project) -> list[tuple[str, str, str]]:
    return official_operation_inventory_queries(project, settings=get_settings())


def _ensure_profile(project: Project) -> None:
    if project.topic_profile:
        return
    project.topic_profile = build_topic_profile(project.topic)
    project.project_type = project.topic_profile["project_type"]


def _persist_execution_plan(project: Project, raw: dict[str, Any] | None) -> dict[str, Any]:
    plan = sanitize_execution_plan(project, raw)
    project.execution_plan = plan
    return plan


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    """Deterministic fallback when the planning LLM is unavailable."""
    _ensure_profile(project)
    settings = get_settings()
    _persist_execution_plan(project, heuristic_execution_plan(project))
    _, flags = execution_flags(project)
    fallback = MediaScout(project.topic, project.topic_profile).fallback_search_strategy(
        max_complementary=settings.max_complementary_queries
    )
    strategy = _sanitize_strategy(
        project,
        fallback,
        enable_fact_layer=flags["enable_fact_layer"],
    )
    return _persist_strategy_queries(db, project, strategy)


def plan_report_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    """Plan the methodology and the compact search strategy in one LLM call."""
    _ensure_profile(project)
    settings = get_settings()

    if not llm_is_configured():
        return plan_queries(db, project)

    facts = db.scalars(
        select(OfficialFact).where(OfficialFact.project_id == project.id)
    ).all()

    payload = {
        "project": project_payload(project),
        "topic_profile": project.topic_profile,
        "requested_execution_profile": project.execution_profile or "AUTO",
        "execution_overrides": project.execution_options or {},
        "reusable_corpus": (project.topic_profile or {}).get("corpus_reuse") or {},
        "planning_constraints": {
            "target_valid_media_items_after_validation": settings.target_media_items,
            "collection_preserves_all_returned_hits": True,
            "collection_is_metadata_and_snippet_only": True,
            "full_article_hydration_happens_during_media_validation": True,
            "results_per_query": settings.max_results_per_query,
            "max_complementary_queries": settings.max_complementary_queries,
            "priority_portal_queries_are_generated_by_code": True,
            "nominal_followup_is_optional": True,
            "reuse_historical_corpus_before_new_search": True,
            "isp_mention_is_not_required_for_media_relevance": True,
            "new_search_should_fill_gaps_in_existing_corpus": True,
        },
        "official_facts": [
            {
                "label": fact.label,
                "value": fact.value,
                "indicator": fact.indicator,
                "geography": fact.geography,
                "period_start": fact.period_start.isoformat() if fact.period_start else None,
                "period_end": fact.period_end.isoformat() if fact.period_end else None,
                "source_reference": fact.source_reference,
                "evidence": fact.evidence,
            }
            for fact in facts
        ],
    }

    try:
        result = get_report_agent().run(
            task="report_planner",
            payload=payload,
            schema_name="report_plan_v1",
            response_model=ReportPlanResponse,
            max_output_tokens=3800,
        )
    except RuntimeError:
        return plan_queries(db, project)

    process_raw = {
        "processes": {
            "web_collection": result["web_collection"],
            "youtube_collection": result["youtube_collection"],
            "social_repercussion": (
                result.get("social_repercussion")
                or {
                    "enabled": False,
                    "reason": "Planejador nao solicitou coleta social.",
                }
            ),
            "academic_research": result["academic_research"],
            "media_validation": result["media_validation"],
            "fact_extraction": result["fact_extraction"],
            "fact_resolution": result["fact_resolution"],
            "nominal_followup": result["nominal_followup"],
            "second_fact_pass": result["second_fact_pass"],
            "classification": result["classification"],
            "report_writer": result["report_writer"],
            "qa": result["qa"],
        },
        "fact_fields": result.get("fact_fields") or [],
        "rationale": result.get("rationale") or "",
    }
    _persist_execution_plan(project, process_raw)
    db.flush()
    _, flags = execution_flags(project)

    strategy = _sanitize_strategy(
        project,
        result,
        enable_fact_layer=flags["enable_fact_layer"],
    )
    created = _persist_strategy_queries(db, project, strategy)
    db.commit()
    return created


def plan_queries_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    """Backward-compatible API name for the new report planner."""
    return plan_report_with_llm(db, project)


# ---------------------------------------------------------------------------
# Fase 2: cobertura complementar direcionada a lacunas
# ---------------------------------------------------------------------------

# Resultados de portal_checks que justificam uma segunda tentativa direcionada.
# "coleta desativada/indisponível" e cobertura confirmada ficam de fora.

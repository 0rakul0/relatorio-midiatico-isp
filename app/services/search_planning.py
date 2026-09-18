from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.media_scout import MediaScout, ScoutTask
from app.models import OfficialFact, Project, SearchQuery
from app.schemas import ReportPlanResponse
from app.source_registry import OFFICIAL_SECURITY_SOURCES, PRIORITY_MEDIA_SOURCES
from app.topic_profile import build_topic_profile, normalized_text
from app.services.collection.guards import query_preserves_project_anchor
from app.services.execution_profile import (
    execution_flags,
    heuristic_execution_plan,
    sanitize_execution_plan,
)
from app.services.project_profile import project_payload


_INITIAL_PURPOSES = {"MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"}
_STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma",
}


def _existing_queries(db: Session, project_id: int) -> set[str]:
    return set(
        db.scalars(
            select(SearchQuery.query).where(SearchQuery.project_id == project_id)
        ).all()
    )


def _query_tokens(query: str) -> set[str]:
    # site: constraints are deterministic coverage hints and should not make two
    # otherwise equal semantic queries look different.
    text = re.sub(r"(?:^|\s)site:[^\s]+", " ", query or "")
    text = normalized_text(text)
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in _STOPWORDS and len(token) > 1
    }


def _semantic_similarity(left: str, right: str) -> float:
    a = _query_tokens(left)
    b = _query_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_redundant(candidate: str, selected: list[str], threshold: float = 0.84) -> bool:
    normalized = " ".join(normalized_text(candidate).split())
    for previous in selected:
        if normalized == " ".join(normalized_text(previous).split()):
            return True
        if _semantic_similarity(candidate, previous) >= threshold:
            return True
    return False


def _media_profile_tokens(project: Project) -> set[str]:
    profile = project.topic_profile or {}
    values = [project.topic]
    for key in (
        "product_name", "product_anchor", "event_anchor",
        "product_search_variants", "event_search_variants", "subject_terms",
        "actors", "actions", "locations", "organizations", "search_synonyms",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    tokens: set[str] = set()
    for value in values:
        tokens.update(_query_tokens(value))
    return tokens


def _media_query_is_acceptable(project: Project, query: str) -> bool:
    if query_preserves_project_anchor(project, query, purpose="MEDIA_REPERCUSSION"):
        return True
    query_tokens = _query_tokens(query)
    profile_tokens = _media_profile_tokens(project)
    if not query_tokens or not profile_tokens:
        return False
    overlap = query_tokens.intersection(profile_tokens)
    return len(overlap) >= 2 or (
        len(overlap) == 1 and len(query_tokens) <= 4 and len(profile_tokens) <= 6
    )


def _query_is_acceptable(project: Project, query: str, *, purpose: str) -> bool:
    if purpose == "MEDIA_REPERCUSSION":
        return _media_query_is_acceptable(project, query)
    return query_preserves_project_anchor(project, query, purpose=purpose)


def _add_query(
    db: Session,
    project: Project,
    existing: set[str],
    *,
    query: str,
    kind: str,
    purpose: str,
    rationale: str,
    priority: int,
) -> SearchQuery | None:
    query = " ".join(query.split()).strip()
    if not query or query in existing:
        return None
    if not _query_is_acceptable(project, query, purpose=purpose):
        return None
    row = SearchQuery(
        project_id=project.id,
        query=query,
        kind=kind,
        purpose=purpose,
        rationale=rationale,
        priority=priority,
    )
    db.add(row)
    existing.add(query)
    return row


def _store_strategy(project: Project, strategy: dict[str, Any]) -> None:
    profile = dict(project.topic_profile or {})
    profile["search_strategy"] = {
        "primary_query": strategy["primary_query"],
        "complementary_queries": list(strategy.get("complementary_queries") or []),
        "fact_query": strategy.get("fact_query"),
        "official_query": strategy.get("official_query"),
        "rationale": strategy.get("rationale") or "",
    }
    project.topic_profile = profile


def _sanitize_strategy(
    project: Project,
    raw: dict[str, Any] | None,
    *,
    enable_fact_layer: bool,
) -> dict[str, Any]:
    settings = get_settings()
    scout = MediaScout(project.topic, project.topic_profile)
    fallback = scout.fallback_search_strategy(
        max_complementary=settings.max_complementary_queries
    )
    raw = raw or {}

    primary = " ".join(str(raw.get("primary_query") or "").split()).strip()
    if not primary or not _media_query_is_acceptable(project, primary):
        primary = str(fallback["primary_query"])

    selected = [primary]
    complementary: list[str] = []
    for candidate in raw.get("complementary_queries") or []:
        candidate = " ".join(str(candidate or "").split()).strip()
        if not candidate:
            continue
        if not _media_query_is_acceptable(project, candidate):
            continue
        if _is_redundant(candidate, selected):
            continue
        complementary.append(candidate)
        selected.append(candidate)
        if len(complementary) >= settings.max_complementary_queries:
            break

    # If the model produced no useful complement, deterministic fallback may
    # contribute at most the configured number of genuinely distinct variants.
    if len(complementary) < settings.max_complementary_queries:
        for candidate in fallback.get("complementary_queries") or []:
            if len(complementary) >= settings.max_complementary_queries:
                break
            candidate = " ".join(str(candidate or "").split()).strip()
            if not candidate or _is_redundant(candidate, selected):
                continue
            if not _media_query_is_acceptable(project, candidate):
                continue
            complementary.append(candidate)
            selected.append(candidate)

    fact_query = None
    official_query = None
    if enable_fact_layer:
        candidate = " ".join(str(raw.get("fact_query") or "").split()).strip()
        if candidate and query_preserves_project_anchor(
            project, candidate, purpose="FACT_DISCOVERY"
        ):
            fact_query = candidate
        else:
            fallback_fact = str(fallback.get("fact_query") or "").strip()
            if fallback_fact and query_preserves_project_anchor(
                project, fallback_fact, purpose="FACT_DISCOVERY"
            ):
                fact_query = fallback_fact

        candidate = " ".join(str(raw.get("official_query") or "").split()).strip()
        if candidate and query_preserves_project_anchor(
            project, candidate, purpose="OFFICIAL_FACT"
        ):
            official_query = candidate
        else:
            fallback_official = str(fallback.get("official_query") or "").strip()
            if fallback_official and query_preserves_project_anchor(
                project, fallback_official, purpose="OFFICIAL_FACT"
            ):
                official_query = fallback_official

    return {
        "primary_query": primary,
        "complementary_queries": complementary,
        "fact_query": fact_query,
        "official_query": official_query,
        "rationale": str(raw.get("rationale") or fallback.get("rationale") or "").strip(),
    }


def _replace_pending_initial_plan(db: Session, project_id: int) -> None:
    """Remove only never-executed initial-plan queries.

    Executed queries remain immutable audit evidence. Nominal follow-up queries
    are managed by the fact layer and are not touched here.
    """
    db.execute(
        delete(SearchQuery).where(
            SearchQuery.project_id == project_id,
            SearchQuery.executed_at.is_(None),
            SearchQuery.purpose.in_(tuple(_INITIAL_PURPOSES)),
        )
    )
    db.flush()


def _media_task_order(tasks: list[ScoutTask]) -> list[ScoutTask]:
    primary = [task for task in tasks if task.role == "PRIMARY"]
    portals = [task for task in tasks if task.role == "PRIORITY_PORTAL"]
    complementary = [task for task in tasks if task.role == "COMPLEMENTARY"]
    other = [
        task
        for task in tasks
        if task.role not in {"PRIMARY", "PRIORITY_PORTAL", "COMPLEMENTARY"}
    ]
    return [*primary, *portals, *complementary, *other]


def _persist_strategy_queries(
    db: Session,
    project: Project,
    strategy: dict[str, Any],
) -> list[SearchQuery]:
    settings = get_settings()
    _, flags = execution_flags(project)

    _replace_pending_initial_plan(db, project.id)
    _store_strategy(project, strategy)
    db.flush()

    existing = _existing_queries(db, project.id)
    created: list[SearchQuery] = []

    scout = MediaScout(project.topic, project.topic_profile)
    media_tasks = _media_task_order(
        scout.web_tasks(max_complementary=settings.max_complementary_queries)
    )

    reuse = (project.topic_profile or {}).get("corpus_reuse") or {}
    covered_domains = {str(value).lower() for value in (reuse.get("covered_domains") or [])}
    portal_domains = {label: domain.lower() for label, domain in PRIORITY_MEDIA_SOURCES}
    strong_exact_reuse = bool(
        reuse.get("exact_topic_match")
        and int(reuse.get("reused") or 0) >= settings.corpus_reuse_sufficient_items
    )
    filtered_tasks: list[ScoutTask] = []
    for task in media_tasks:
        if task.role == "PRIORITY_PORTAL":
            domain = portal_domains.get(task.target, "")
            if domain and any(
                covered == domain or covered.endswith("." + domain)
                for covered in covered_domains
            ):
                continue
        if task.role == "COMPLEMENTARY" and strong_exact_reuse:
            continue
        filtered_tasks.append(task)
    media_tasks = filtered_tasks

    media_added = 0
    for task in media_tasks:
        if media_added >= settings.max_media_queries:
            break
        if task.role == "PRIMARY":
            kind = "media_primary"
            priority = 1
        elif task.role == "PRIORITY_PORTAL":
            kind = "media_portal"
            priority = 2
        else:
            kind = "media_complementary"
            priority = 3
        row = _add_query(
            db,
            project,
            existing,
            query=task.query,
            kind=kind,
            purpose="MEDIA_REPERCUSSION",
            rationale=task.rationale,
            priority=priority,
        )
        if row:
            created.append(row)
            media_added += 1

    if flags["enable_fact_layer"]:
        fact_query = str(strategy.get("fact_query") or "").strip()
        if settings.max_fact_queries > 0 and fact_query:
            row = _add_query(
                db,
                project,
                existing,
                query=fact_query,
                kind="fact_discovery",
                purpose="FACT_DISCOVERY",
                rationale="Discover individual occurrences and factual corroboration.",
                priority=1,
            )
            if row:
                created.append(row)

        official_base = str(strategy.get("official_query") or "").strip()
        official_added = 0
        if official_base:
            for source in OFFICIAL_SECURITY_SOURCES:
                if official_added >= settings.max_official_queries:
                    break
                row = _add_query(
                    db,
                    project,
                    existing,
                    query=f'site:{source["domain"]} {official_base}',
                    kind="official",
                    purpose="OFFICIAL_FACT",
                    rationale=f'Check primary institutional evidence at {source["label"]}.',
                    priority=1,
                )
                if row:
                    created.append(row)
                    official_added += 1

    db.commit()
    return created


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
            "cross_validation": result["cross_validation"],
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

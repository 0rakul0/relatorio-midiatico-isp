from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import get_settings
from app.media_scout import MediaScout, ScoutTask
from app.models import Project, SearchQuery
from app.search.guards import (
    is_redundant as _is_redundant,
    media_query_is_acceptable as _media_query_is_acceptable,
)
from app.search.inventory import (
    annual_event_inventory_queries,
    official_operation_inventory_queries,
)
from app.search.persistence import add_query as _add_query, existing_queries as _existing_queries
from app.services.collection.guards import query_preserves_project_anchor
from app.services.execution_profile import execution_flags
from app.source_registry import OFFICIAL_SECURITY_SOURCES, PRIORITY_MEDIA_SOURCES
from app.topic_profile import canonicalize_known_locations


_INITIAL_PURPOSES = {"MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"}


def _annual_event_inventory_queries(project: Project) -> list[tuple[str, str]]:
    return annual_event_inventory_queries(project, settings=get_settings())


def _official_operation_inventory_queries(project: Project) -> list[tuple[str, str, str]]:
    return official_operation_inventory_queries(project, settings=get_settings())


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
    if primary:
        # Normaliza aliases geográficos conhecidos também quando a estratégia
        # veio da LLM. Assim, a grafia digitada pelo usuário não vira a âncora
        # propagada para todos os portais prioritários.
        primary = canonicalize_known_locations(primary)
    if not primary or not _media_query_is_acceptable(project, primary):
        primary = str(fallback["primary_query"])

    selected = [primary]
    complementary: list[str] = []
    for candidate in raw.get("complementary_queries") or []:
        candidate = " ".join(str(candidate or "").split()).strip()
        if candidate:
            candidate = canonicalize_known_locations(candidate)
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

    # Redes sociais nao recebem consultas de descoberta proprias. O DuckDuckGo
    # executa o plano midiatico principal; URLs sociais encontradas nessa mesma
    # descoberta sao classificadas e enriquecidas posteriormente pelo Apify.

    if flags["enable_fact_layer"]:
        # Para inventários anuais, a consulta única do planejador é
        # complementada por uma varredura mensal determinística. Isso aumenta
        # recall sem remover os limites globais nem gerar paráfrases infinitas.
        for query, rationale in _annual_event_inventory_queries(project):
            row = _add_query(
                db,
                project,
                existing,
                query=query,
                kind="fact_inventory_month",
                purpose="FACT_DISCOVERY",
                rationale=rationale,
                priority=1,
            )
            if row:
                created.append(row)

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

        # Varredura oficial mensal: primeiro descobrimos operações em releases
        # primários; depois a mídia é usada para medir repercussão. Não depende
        # da LLM inventar dezenas de consultas.
        official_added = 0
        for query, kind, rationale in _official_operation_inventory_queries(project):
            if official_added >= settings.max_official_queries:
                break
            row = _add_query(
                db,
                project,
                existing,
                query=query,
                kind=kind,
                purpose="OFFICIAL_FACT",
                rationale=rationale,
                priority=1,
            )
            if row:
                created.append(row)
                official_added += 1

        official_base = str(strategy.get("official_query") or "").strip()
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


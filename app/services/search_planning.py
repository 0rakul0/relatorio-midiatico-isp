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


def plan_missing_month_operation_inventory_queries(
    db: Session,
    project: Project,
    months: list[str],
) -> list[SearchQuery]:
    """Retorna consultas mensais pendentes e cria variantes quando necessario."""
    wanted = {
        str(value)
        for value in months
        if isinstance(value, str) and len(value) == 7 and value[4] == "-"
    }
    if not wanted or not project.event_start or not project.event_end:
        return []

    existing_rows = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project.id)
        .order_by(SearchQuery.id.asc())
    ).all()
    existing = {row.query for row in existing_rows}
    ready: list[SearchQuery] = []

    def month_key_for_query(query: str) -> str | None:
        normalized_query = normalized_text(query)
        for month_index, month_name in enumerate(_PT_MONTHS, start=1):
            if month_name not in normalized_query:
                continue
            for year in range(project.event_start.year, project.event_end.year + 1):
                if str(year) in normalized_query:
                    return f"{year:04d}-{month_index:02d}"
        return None

    # Reaproveita consultas mensais ja existentes e ainda nao executadas.
    for row in existing_rows:
        if (
            row.executed_at is None
            and row.kind in {"fact_inventory_month", "official_operation_inventory"}
            and month_key_for_query(row.query) in wanted
        ):
            ready.append(row)

    # Cria consultas-base ausentes.
    for query, rationale in _annual_event_inventory_queries(project):
        if month_key_for_query(query) not in wanted:
            continue
        row = _add_query(
            db, project, existing,
            query=query,
            kind="fact_inventory_month",
            purpose="FACT_DISCOVERY",
            rationale=f"[refinamento inventario] {rationale}",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

    for query, kind, rationale in _official_operation_inventory_queries(project):
        if month_key_for_query(query) not in wanted:
            continue
        row = _add_query(
            db, project, existing,
            query=query,
            kind=kind,
            purpose="OFFICIAL_FACT",
            rationale=f"[refinamento inventario] {rationale}",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

    # Se as consultas-base ja foram executadas e o mes continua vazio,
    # abre variantes de recall sem apagar a trilha das tentativas anteriores.
    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "operacoes policiais").strip()
    locations = profile.get("locations") or ["Rio de Janeiro"]
    location = str(locations[0] or "Rio de Janeiro")

    for month_key in sorted(wanted):
        year = int(month_key[:4])
        month_index = int(month_key[5:7])
        month_name = _PT_MONTHS[month_index - 1]
        existing_for_month = [
            row for row in existing_rows
            if month_key_for_query(row.query) == month_key
            and row.kind in {"fact_inventory_month", "official_operation_inventory"}
        ]
        if any(row.executed_at is None for row in existing_for_month):
            continue

        attempts = len(existing_for_month)
        open_variants = [
            f'"{anchor}" "{location}" {month_name} {year} balanco operacao',
            f'"operacao policial" "{location}" {month_name} {year} mortos presos apreensoes',
            f'"operacoes policiais" RJ {month_name} {year} policia civil militar',
        ]
        open_query = open_variants[min(attempts, len(open_variants) - 1)]
        row = _add_query(
            db, project, existing,
            query=open_query,
            kind="fact_inventory_month",
            purpose="FACT_DISCOVERY",
            rationale=f"[refinamento inventario] Variante de recall para {month_name}/{year}.",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

        for source in OFFICIAL_OPERATION_INVENTORY_SOURCES:
            domain = source["domain"]
            official_variants = [
                f'site:{domain} operacao {month_name} {year} Rio de Janeiro',
                f'site:{domain} "operacao policial" {month_name} {year}',
                f'site:{domain} operacao balanco {month_name} {year}',
            ]
            query = official_variants[min(attempts, len(official_variants) - 1)]
            row = _add_query(
                db, project, existing,
                query=query,
                kind="official_operation_inventory",
                purpose="OFFICIAL_FACT",
                rationale=(
                    f"[refinamento inventario] Variante oficial {source['label']} "
                    f"para {month_name}/{year}."
                ),
                priority=1,
            )
            if row:
                db.flush()
                ready.append(row)

    db.commit()
    unique: list[SearchQuery] = []
    seen: set[int] = set()
    for row in ready:
        marker = int(row.id or 0)
        if marker and marker in seen:
            continue
        if marker:
            seen.add(marker)
        unique.append(row)
    return unique

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

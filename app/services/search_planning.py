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
from app.schemas import GapFillResponse, ReportPlanResponse
from app.source_registry import (
    OFFICIAL_OPERATION_INVENTORY_SOURCES,
    OFFICIAL_SECURITY_SOURCES,
    PRIORITY_MEDIA_SOURCES,
)
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


# Vocabulário jornalístico para uma segunda tentativa quando a primeira rodada
# termina com corpus midiático zero. É um fallback determinístico, conservador
# e auditável; o agente pode propor outros ângulos antes dele.
_ZERO_RECOVERY_EXPANSIONS: dict[str, list[str]] = {
    "habitacional": ["imoveis", "construcao", "mercado imobiliario", "moradia"],
    "habitacao": ["imoveis", "construcao", "mercado imobiliario", "moradia"],
    "moradia": ["imoveis", "construcao", "mercado imobiliario", "habitacao"],
    "imobiliario": ["imoveis", "construcao", "mercado imobiliario"],
    "imobiliaria": ["imoveis", "construcao", "mercado imobiliario"],
    "milicia": ["milicia", "milicianos"],
    "miliciano": ["milicia", "milicianos"],
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


_PT_MONTHS = [
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _annual_event_inventory_queries(project: Project) -> list[tuple[str, str]]:
    """Varredura mensal para pautas anuais de operações/eventos recorrentes."""
    settings = get_settings()
    if not settings.enable_annual_event_inventory:
        return []
    if project.project_type != "EVENT_TOPIC" or not project.event_start or not project.event_end:
        return []
    if (project.event_end - project.event_start).days < 180:
        return []

    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "").strip()
    anchor_norm = normalized_text(anchor)
    topic_norm = normalized_text(project.topic)
    # Ativa automaticamente para inventários de operações policiais.
    if not ("operac" in anchor_norm or "operac" in topic_norm):
        return []
    if not ("polic" in anchor_norm or "polic" in topic_norm):
        return []

    locations = profile.get("locations") or ["Rio de Janeiro"]
    location = str(locations[0] or "Rio de Janeiro")
    start = project.event_start
    end = project.event_end
    queries: list[tuple[str, str]] = []
    year_month = (start.year, start.month)
    while year_month <= (end.year, end.month):
        year, month = year_month
        month_name = _PT_MONTHS[month - 1]
        query = f'"{anchor}" "{location}" {month_name} {year}'
        queries.append((query, f"Inventário mensal de operações policiais: {month_name}/{year}."))
        if len(queries) >= settings.max_annual_event_inventory_queries:
            break
        month += 1
        if month == 13:
            year += 1
            month = 1
        year_month = (year, month)
    return queries


def _official_operation_inventory_queries(project: Project) -> list[tuple[str, str, str]]:
    """Descoberta mensal em fontes primárias para inventários anuais.

    Retorna (query, kind, rationale). A mídia continua separada: estas consultas
    alimentam FACT/official evidence, não métricas de repercussão.
    """
    settings = get_settings()
    if not settings.enable_official_annual_inventory:
        return []
    if not project.event_start or not project.event_end:
        return []
    if (project.event_end - project.event_start).days < 180:
        return []

    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "").strip()
    normalized = normalized_text(anchor + " " + project.topic)
    if "operac" not in normalized or "polic" not in normalized:
        return []

    rows: list[tuple[str, str, str]] = []
    year_month = (project.event_start.year, project.event_start.month)
    while year_month <= (project.event_end.year, project.event_end.month):
        year, month = year_month
        month_name = _PT_MONTHS[month - 1]
        for source in OFFICIAL_OPERATION_INVENTORY_SOURCES:
            query = f'site:{source["domain"]} operacao policial {month_name} {year}'
            rows.append((
                query,
                "official_operation_inventory",
                f'Inventário oficial mensal de operações: {source["label"]}, {month_name}/{year}.',
            ))
            if len(rows) >= settings.max_official_inventory_queries:
                return rows
        month += 1
        if month == 13:
            year += 1
            month = 1
        year_month = (year, month)
    return rows


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

    if flags.get("enable_social_repercussion"):
        # DuckDuckGo continua como mecanismo de descoberta. Estas consultas
        # localizam posts publicos que depois podem ter comentarios
        # enriquecidos pelo Apify.
        social_sources = [
            ("instagram", "instagram.com"),
            ("facebook", "facebook.com"),
            ("x", "x.com"),
        ]
        social_base = str(strategy.get("primary_query") or project.topic).strip()
        social_added = 0
        for platform, domain in social_sources:
            if social_added >= settings.max_social_discovery_queries:
                break
            row = _add_query(
                db,
                project,
                existing,
                query=f"site:{domain} {social_base}",
                kind=f"social_{platform}",
                purpose="MEDIA_REPERCUSSION",
                rationale=(
                    f"Descobrir posts publicos no {platform} para analise "
                    "de repercussao social."
                ),
                priority=2,
            )
            if row:
                created.append(row)
                social_added += 1

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
_GAP_FILLABLE_RESULTS = {
    "sem item validado na amostra",
    "não consultado individualmente nesta execução",
}


def detect_coverage_gaps(db: Session, project: Project) -> dict[str, Any]:
    """Lacunas acionáveis: portais prioritários sem item validado (ordem fixa).

    YouTube fica de fora: os canais prioritários já passam por auditoria
    dedicada na primeira coleta e repetí-los não traz cobertura nova.
    """
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
        if checks.get(label, "") in _GAP_FILLABLE_RESULTS
    ]
    valid_items = int(data.get("valid_items") or 0)
    zero_corpus = valid_items == 0
    return {
        "uncovered_portals": uncovered,
        "valid_items": valid_items,
        "target_items": int(settings.target_media_items),
        "zero_corpus": zero_corpus,
        # Corpus zero sempre exige uma segunda estratégia aberta antes de
        # aceitarmos "0 itens", mesmo quando portal_checks não trouxe lacunas
        # individualizadas (por exemplo, provedor retornou zero globalmente).
        "needs_fill": bool(uncovered) or zero_corpus,
    }


def _has_site_operator(query: str) -> bool:
    return bool(re.search(r"(?:^|\s)site:[^\s]+", query or "", flags=re.IGNORECASE))


def _gap_fallback_angles(
    project: Project, executed: list[str], cap: int
) -> list[tuple[str, str]]:
    """Ângulos abertos ainda não executados, minerados das listas do perfil.

    O round 1 já consumiu a estratégia compacta do scout; aqui varremos as
    variantes restantes (descoberta factual, assunto, sinônimos, âncoras)
    em busca de formulações semanticamente novas para a web como um todo.
    """
    profile = project.topic_profile or {}
    pools: list[str] = []
    for key in (
        "event_search_variants",
        "fact_discovery_variants",
        "product_search_variants",
        "subject_terms",
        "search_synonyms",
        "event_anchor",
        "product_anchor",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            pools.extend(str(item) for item in value if str(item).strip())
        elif value:
            pools.append(str(value))
    pools.append(project.topic)

    seen: set[str] = set()
    phrases: list[str] = []
    for phrase in pools:
        compact = " ".join(str(phrase).split()).strip()
        key = normalized_text(compact)
        if compact and key not in seen:
            seen.add(key)
            phrases.append(compact)

    selected = list(executed)
    output: list[tuple[str, str]] = []
    for phrase in phrases:
        if len(output) >= max(0, cap):
            break
        if not _media_query_is_acceptable(project, phrase):
            continue
        if _is_redundant(phrase, selected):
            continue
        output.append((phrase, "Ângulo do perfil ainda não executado na web aberta."))
        selected.append(phrase)
    return output


def _zero_corpus_fallback_angles(
    project: Project,
    executed: list[str],
    cap: int,
) -> list[tuple[str, str]]:
    """Gera consultas mais amplas quando a primeira validação terminou em zero.

    Prioriza a localidade canônica do perfil e troca linguagem acadêmica/formal
    por termos comuns de manchetes. A consulta original continua preservada nas
    SearchQuery anteriores, portanto a expansão não apaga a trilha de auditoria.
    """
    if cap <= 0:
        return []

    profile = project.topic_profile or {}
    locations = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("locations") or [])
        if str(value).strip()
    ]
    location = locations[0] if locations else ""
    topic_norm = normalized_text(project.topic or "")
    topic_tokens = [
        token for token in re.findall(r"[a-z0-9]+", topic_norm)
        if token not in _STOPWORDS and len(token) > 2
    ]

    # Termos discriminantes que não são apenas a localidade.
    location_tokens: set[str] = set()
    for value in locations:
        location_tokens.update(_query_tokens(value))
    core_tokens = [
        token for token in topic_tokens
        if token not in location_tokens and token not in {"producao", "perfil", "tema"}
    ]

    expansions: list[str] = []
    for token in topic_tokens:
        expansions.extend(_ZERO_RECOVERY_EXPANSIONS.get(token, []))

    # Evita consultas de uma palavra só. Mantém uma âncora territorial quando
    # conhecida e, em seguida, combina até dois termos discriminantes.
    anchor = f'"{location}"' if location else ""
    base_core = [token for token in core_tokens if token not in {"habitacional", "habitacao", "moradia", "imobiliario", "imobiliaria"}]
    if not base_core:
        base_core = core_tokens[:2]

    candidates: list[str] = []
    for expansion in list(dict.fromkeys(expansions)):
        parts = [anchor, *base_core[:1], expansion]
        query = " ".join(part for part in parts if part).strip()
        if query:
            candidates.append(query)

    # Último fallback: local + dois conceitos centrais. É mais amplo que a
    # consulta original, mas ainda preserva contexto suficiente para validação.
    broad_parts = [anchor, *base_core[:2]]
    broad = " ".join(part for part in broad_parts if part).strip()
    if broad:
        candidates.append(broad)

    selected = list(executed)
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = " ".join(normalized_text(candidate).split())
        if not key or key in seen:
            continue
        seen.add(key)
        if not _media_query_is_acceptable(project, candidate):
            continue
        if _is_redundant(candidate, selected):
            continue
        output.append(
            (
                candidate,
                "Recuperação obrigatória após corpus zero: linguagem jornalística/consulta mais ampla.",
            )
        )
        selected.append(candidate)
        if len(output) >= cap:
            break
    return output


def plan_gap_fill_queries(
    db: Session,
    project: Project,
    gaps: dict[str, Any],
    *,
    max_queries: int | None = None,
) -> list[SearchQuery]:
    """Planeja (LLM + guardas) consultas abertas para as lacunas. Idempotente.

    As consultas pesquisam a web como um todo — sem operador site: nem
    restrição a links. As lacunas entram como contexto do que falta cobrir.
    """
    settings = get_settings()
    cap = max(0, int(settings.max_gap_fill_queries if max_queries is None else max_queries))
    uncovered = list(gaps.get("uncovered_portals") or [])
    zero_corpus = bool(gaps.get("zero_corpus"))
    if cap <= 0 or (not uncovered and not zero_corpus):
        return []

    executed = [
        row.query for row in db.scalars(
            select(SearchQuery).where(SearchQuery.project_id == project.id)
        ).all()
    ]
    existing = _existing_queries(db, project.id)
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
                        if zero_corpus else None
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
            if not query or _has_site_operator(query):
                continue
            if not _media_query_is_acceptable(project, query):
                continue
            if query in existing or _is_redundant(query, selected):
                continue
            candidates.append((query, str(item.get("rationale") or "")))
            selected.append(query)

    # Em corpus zero, a prioridade é sair da formulação que já falhou:
    # grafias alternativas + vocabulário de manchetes + consulta mais ampla.
    if zero_corpus and len(candidates) < cap:
        for query, rationale in _zero_corpus_fallback_angles(
            project, selected, cap - len(candidates)
        ):
            if query in existing:
                continue
            candidates.append((query, rationale))
            selected.append(query)

    # Fallback geral: ângulos do perfil ainda não executados.
    if len(candidates) < cap:
        for query, rationale in _gap_fallback_angles(project, selected, cap - len(candidates)):
            if query in existing:
                continue
            candidates.append((query, rationale))
            selected.append(query)

    created: list[SearchQuery] = []
    for query, rationale in candidates[:cap]:
        row = _add_query(
            db,
            project,
            existing,
            query=query,
            kind="media_zero_recovery" if zero_corpus else "media_complementary",
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

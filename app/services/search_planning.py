from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.media_scout import MediaScout
from app.models import OfficialFact, Project, SearchQuery
from app.schemas import SearchPlanResponse
from app.source_registry import OFFICIAL_SECURITY_SOURCES
from app.topic_profile import build_topic_profile, normalized_text
from app.year_utils import find_year
from app.services.execution_profile import execution_flags
from app.services.project_profile import project_payload
from app.services.collection.guards import query_preserves_project_anchor


def _existing_queries(db: Session, project_id: int) -> set[str]:
    return set(db.scalars(select(SearchQuery.query).where(SearchQuery.project_id == project_id)).all())


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
    # Hard guard central: consultas de repercussão/fato precisam preservar
    # a identidade do objeto monitorado. Produto institucional usa âncora
    # nominal; EVENT_TOPIC conhecido usa âncora factual + local/período.
    if not query_preserves_project_anchor(project, query, purpose=purpose):
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


def _fact_query_bases(project: Project) -> list[str]:
    profile = project.topic_profile or {}

    # Eventos com âncora factual usam variantes controladas. Isso evita gerar
    # combinações vagas como "morte Rio" ou "polícia 2026".
    if project.project_type == "EVENT_TOPIC" and (
        profile.get("event_anchor") or profile.get("fact_discovery_variants")
    ):
        variants = [
            str(value).strip()
            for value in (
                profile.get("fact_discovery_variants")
                or profile.get("event_search_variants")
                or [profile.get("event_anchor")]
            )
            if str(value or "").strip()
        ]
        locations = [str(value).strip() for value in (profile.get("locations") or []) if str(value).strip()]
        location = next((value for value in locations if len(value) > 2), "")
        year = ""
        if project.event_start and project.event_end and project.event_start.year == project.event_end.year:
            year = str(project.event_start.year)
        else:
            year = find_year(project.topic or "") or ""

        bases: list[str] = []
        for variant in variants[:8]:
            query = f'"{variant}"'
            if location and normalized_text(location) not in normalized_text(variant):
                query += f' "{location}"'
            if year and year not in variant:
                query += f" {year}"
            bases.append(query.strip())
        return list(dict.fromkeys(bases))[:8]

    actors = (profile.get("actors") or [])[:5]
    actions = (profile.get("actions") or [])[:5]
    locations = (profile.get("locations") or [])[:3]
    bases: list[str] = [project.topic]
    if actors and actions:
        for actor in actors[:5]:
            for action in actions[:4]:
                parts = [actor, action, *locations[:1]]
                bases.append(" ".join(part for part in parts if part))
    bases.extend((profile.get("search_synonyms") or [])[:8])
    return list(dict.fromkeys(base.strip() for base in bases if base.strip()))[:24]


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    if not project.topic_profile:
        project.topic_profile = build_topic_profile(project.topic)
        project.project_type = project.topic_profile["project_type"]

    settings = get_settings()
    query_limit = max(1, settings.max_search_queries)
    existing = _existing_queries(db, project.id)
    created: list[SearchQuery] = []
    _, flags = execution_flags(project)

    scout_tasks = MediaScout(project.topic, project.topic_profile).web_tasks()
    thematic_tasks = [task for task in scout_tasks if not task.is_priority]
    portal_tasks = [task for task in scout_tasks if task.is_priority]

    def add_media_task(task) -> None:
        if len(created) >= query_limit:
            return
        row = _add_query(
            db,
            project,
            existing,
            query=task.query,
            kind="media_scout_web",
            purpose="MEDIA_REPERCUSSION",
            rationale=task.rationale,
            priority=2,
        )
        if row:
            created.append(row)

    # Em EVENT_TOPIC com camada factual, reservamos orçamento desde o início
    # para descoberta de casos e fontes oficiais. Sem isso, as checagens de
    # portais podem consumir MAX_SEARCH_QUERIES inteiro antes da camada factual.
    if project.project_type == "EVENT_TOPIC" and flags["enable_fact_layer"]:
        for task in thematic_tasks[:2]:
            add_media_task(task)

        bases = _fact_query_bases(project)
        fact_budget = min(4, max(0, query_limit - len(created)))
        fact_added = 0
        for base in bases:
            if len(created) >= query_limit or fact_added >= fact_budget:
                break
            row = _add_query(
                db,
                project,
                existing,
                query=base,
                kind="fact_discovery",
                purpose="FACT_DISCOVERY",
                rationale="Descobrir ocorrências individuais e fontes que documentem o fato",
                priority=1,
            )
            if row:
                created.append(row)
                fact_added += 1

        compact = bases[0] if bases else project.topic
        official_budget = min(3, max(0, query_limit - len(created)))
        official_added = 0
        for source in OFFICIAL_SECURITY_SOURCES:
            if len(created) >= query_limit or official_added >= official_budget:
                break
            row = _add_query(
                db,
                project,
                existing,
                query=f'site:{source["domain"]} {compact}',
                kind="official",
                purpose="OFFICIAL_FACT",
                rationale=f'Buscar confirmação institucional em {source["label"]}',
                priority=1,
            )
            if row:
                created.append(row)
                official_added += 1

        # O restante do orçamento é usado para checagens nominais de portais.
        for task in portal_tasks:
            if len(created) >= query_limit:
                break
            add_media_task(task)

        # Se houve duplicatas ou alguma consulta foi rejeitada pelo hard guard,
        # aproveitamos vagas remanescentes com variantes temáticas estritas.
        for task in thematic_tasks[2:]:
            if len(created) >= query_limit:
                break
            add_media_task(task)
    else:
        # Perfis exclusivamente midiáticos preservam o comportamento anterior:
        # busca temática principal, portais prioritários e variantes restantes.
        ordered_tasks = [
            *thematic_tasks[:1],
            *portal_tasks,
            *thematic_tasks[1:],
        ]
        for task in ordered_tasks:
            if len(created) >= query_limit:
                break
            add_media_task(task)

    db.commit()
    return created


def plan_queries_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    settings = get_settings()
    deterministic = plan_queries(db, project)
    if not llm_is_configured() or len(deterministic) >= settings.max_search_queries:
        return deterministic

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    _, flags = execution_flags(project)
    allowed_purposes = ["MEDIA_REPERCUSSION"]
    if project.project_type == "EVENT_TOPIC" and flags["enable_fact_layer"]:
        allowed_purposes.extend(["FACT_DISCOVERY", "OFFICIAL_FACT"])

    result = get_report_agent().run(
        task="search_planner",
        extra_instructions=(
            "Respeite rigorosamente allowed_purposes; não proponha consultas de finalidade desativada. "
            "Quando project.temporal_mode for TOPIC_DRIVEN, NÃO invente datas nem force mês/período. "
            "Planeje consultas a partir do significado da pauta: nome exato do tema, instituição, atores, "
            "ações, locais, sinônimos e combinações úteis para descobrir a repercussão real. "
            "Para produtos institucionais, TODA consulta MEDIA_REPERCUSSION deve preservar a ancora nominal "
            "do produto, preferencialmente entre aspas. Nunca gere consultas com apenas termos genéricos. "
            "Para EVENT_TOPIC com event_anchor, preserve a categoria factual completa. MEDIA_REPERCUSSION e "
            "OFFICIAL_FACT devem usar event_anchor/event_search_variants; FACT_DISCOVERY pode usar também "
            "fact_discovery_variants. Preserve local e ano explícitos e não gere buscas vagas. "
            "Quando houver janela explícita, use-a apenas como contexto temporal."
        ),
        payload={
            "project": project_payload(project),
            "topic_profile": project.topic_profile,
            "allowed_purposes": allowed_purposes,
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
        },
        schema_name="search_plan_v2",
        response_model=SearchPlanResponse,
    )

    existing = _existing_queries(db, project.id)
    created = list(deterministic)
    for item in result.get("queries", []):
        if len(created) >= settings.max_search_queries:
            break
        if item["purpose"] not in allowed_purposes:
            continue
        if not query_preserves_project_anchor(
            project, item["query"], purpose=item["purpose"]
        ):
            continue
        row = _add_query(
            db,
            project,
            existing,
            query=item["query"],
            kind=item["kind"][:40],
            purpose=item["purpose"],
            rationale=item["rationale"],
            priority=item["priority"],
        )
        if row:
            created.append(row)
    db.commit()
    return created



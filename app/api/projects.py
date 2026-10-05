from __future__ import annotations

from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.costs import summarize_costs as _summarize_costs
from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.billing import check_quota, clamp_profile, plan_for
from app.cost_tracker import cost_context
from app.database import get_db
from app.fact_layer import (
    fact_assertions_for_report,
    fact_events_for_main_report,
    fact_events_for_report,
)
from app.models import (
    AcademicPaper,
    Classification,
    FactEvent,
    GeneratedReport,
    LLMCall,
    MediaItem,
    OfficialFact,
    Project,
    SearchQuery,
)
from app.orchestration import (
    request_cancel,
    resume_run,
    run_snapshot,
    start_qa_refinement,
    start_run,
)
from app.report_qa import run_report_qa
from app.schemas import ManualMediaItemCreate, OfficialFactCreate, ProjectCreate
from app.services import (
    cached_report_for_project,
    canonicalize,
    classify_with_llm,
    collect_web,
    discover_project_profile,
    draft_report_with_llm,
    export_report_pdf,
    metrics,
    plan_queries,
    plan_queries_with_llm,
    run_full_methodology,
    validate_and_classify,
)
from app.services.collection.media_origin import classify_media_origin
from app.topic_profile import requested_topic_window


router = APIRouter(tags=["projects"])


BRAZILIAN_STATES = frozenset({
    "Acre", "Alagoas", "Amapá", "Amazonas", "Bahia", "Ceará", "Distrito Federal",
    "Espírito Santo", "Goiás", "Maranhão", "Mato Grosso", "Mato Grosso do Sul",
    "Minas Gerais", "Pará", "Paraíba", "Paraná", "Pernambuco", "Piauí",
    "Rio de Janeiro", "Rio Grande do Norte", "Rio Grande do Sul", "Rondônia",
    "Roraima", "Santa Catarina", "São Paulo", "Sergipe", "Tocantins",
})


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    raw_scopes = payload.geographic_scopes or ([payload.geographic_scope] if payload.geographic_scope else [])
    geographic_scopes = list(dict.fromkeys(
        " ".join(str(scope or "").split()) for scope in raw_scopes
        if " ".join(str(scope or "").split())
    ))
    invalid_scopes = [scope for scope in geographic_scopes if scope not in BRAZILIAN_STATES]
    if invalid_scopes:
        raise HTTPException(422, "Estado inválido para o recorte geográfico")
    geographic_suffix = (
        "" if not geographic_scopes
        else f" no estado de {geographic_scopes[0]}" if len(geographic_scopes) == 1
        else f" nos estados de {', '.join(geographic_scopes)}"
    )
    scoped_topic = f"{payload.topic}{geographic_suffix}"
    if len(scoped_topic) > 300:
        raise HTTPException(422, "Tema e recorte geográfico excedem 300 caracteres")
    today = date.today()
    inferred_window = requested_topic_window(scoped_topic)

    explicit_collection = bool(payload.collection_start or payload.collection_end)
    explicit_event = bool(payload.event_start or payload.event_end)

    # Regra temporal:
    # 1) janela de repercussão explicitamente informada sempre vence;
    # 2) se o usuário informou apenas a janela factual, a repercussão herda
    #    exatamente essa janela (comportamento mais seguro e previsível);
    # 3) caso contrário, um ano/mês explícito no tema define ambas as janelas;
    # 4) sem qualquer recorte, a pesquisa permanece temática.
    if explicit_collection:
        collection_start = payload.collection_start or payload.collection_end
        collection_end = payload.collection_end or payload.collection_start
        collection_window_source = "USER"
        has_custom_window = True
    elif explicit_event:
        collection_start = payload.event_start or payload.event_end
        collection_end = payload.event_end or payload.event_start
        collection_window_source = "EVENT_WINDOW"
        has_custom_window = True
    elif inferred_window:
        collection_start, collection_end = inferred_window
        collection_window_source = "TOPIC"
        has_custom_window = True
    else:
        # Compatibilidade com o schema legado: collection_start/end ainda são
        # NOT NULL no banco, então guardamos hoje apenas como placeholder técnico.
        # has_custom_date_window=False é a fonte de verdade: query_window()
        # devolve (None, None), portanto nenhuma busca recebe filtro de um dia.
        # project_payload() também oculta esse placeholder do relatório final.
        collection_start = collection_end = today
        collection_window_source = "TOPIC_DRIVEN"
        has_custom_window = False

    if explicit_event:
        event_start = payload.event_start or payload.event_end
        event_end = payload.event_end or payload.event_start
        event_window_source = "USER"
    elif inferred_window:
        event_start, event_end = inferred_window
        event_window_source = "TOPIC"
    elif has_custom_window:
        event_start, event_end = collection_start, collection_end
        event_window_source = "COLLECTION_WINDOW"
    else:
        # Sem datas explícitas, não inventamos uma janela factual de um único dia.
        event_start = event_end = None
        event_window_source = "TOPIC_DRIVEN"

    if collection_end < collection_start:
        raise HTTPException(422, "collection_end deve ser posterior ao início")
    if event_start and event_end and event_end < event_start:
        raise HTTPException(422, "event_end deve ser posterior ao início")

    execution_options = {
        key: value
        for key, value in {
            "enable_fact_layer": payload.enable_fact_layer,
            "enable_nominal_followup": payload.enable_nominal_followup,
            "enable_academic_research": payload.enable_academic_research,
        }.items()
        if value is not None
    }
    execution_options["collection_window_source"] = collection_window_source
    execution_options["event_window_source"] = event_window_source
    execution_options["temporal_mode"] = (
        "EXPLICIT_WINDOW" if has_custom_window else "TOPIC_DRIVEN"
    )
    execution_options["geographic_scopes"] = geographic_scopes
    execution_options["geographic_scope"] = geographic_scopes[0] if len(geographic_scopes) == 1 else "NACIONAL"
    # O campo launch_date do banco continua preenchido por compatibilidade com
    # instalações antigas, mas só deve ser exibido como dado editorial quando
    # o usuário o informou ou quando o agente documentalista o confirmou.
    if payload.launch_date is not None:
        execution_options["launch_date_user_supplied"] = True

    # Limites do plano: perfil permitido + recursos vetados.
    plan = plan_for(user)
    execution_profile, plan_notice = clamp_profile(plan, payload.execution_profile)
    if not plan["fact_layer"]:
        execution_options["enable_fact_layer"] = False
    if not plan["nominal_followup"]:
        execution_options["enable_nominal_followup"] = False

    row = Project(
        topic=scoped_topic,
        institution=payload.institution,
        owner_id=user.id,
        launch_date=payload.launch_date or today,
        collection_start=collection_start,
        collection_end=collection_end,
        event_start=event_start,
        event_end=event_end,
        execution_profile=execution_profile,
        execution_options=execution_options,
        fact_grace_days=10,
        has_custom_date_window=has_custom_window,
        project_type="AUTO",
        status="CUSTOM_DATES" if has_custom_window else "DRAFT",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "status": row.status,
        "plan": user.plan,
        "plan_notice": plan_notice,
        "discovery": {
            "status": "DEFERRED",
            "message": "O perfil será executado como a primeira etapa acompanhada do relatório.",
        },
    }


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


@router.post("/projects/{project_id}/discover-profile")
def discover_profile(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    try:
        with cost_context(project_id=project_id, operation="discover_profile"):
            return discover_project_profile(db, project_or_404(db, user, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/run")
def run_project(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    check_quota(db, user)
    try:
        with cost_context(project_id=project_id, operation="run_full_methodology"):
            return run_full_methodology(db, project)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/run-async", status_code=202)
def run_project_async(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    check_quota(db, user)
    return {"run": start_run(project_id)}


def _owned_run_or_404(db: Session, user: AuthUser, run_id: str) -> dict:
    snapshot = run_snapshot(run_id)
    if not snapshot:
        raise HTTPException(404, "Execução não encontrada")
    project_or_404(db, user, snapshot.get("project_id"))
    return snapshot


@router.post("/projects/{project_id}/resume-async", status_code=202)
def resume_project_async(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    check_quota(db, user)
    return {"run": resume_run(project_id)}


@router.post("/projects/{project_id}/refine-qa-async", status_code=202)
def refine_project_qa_async(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    check_quota(db, user)
    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if not saved:
        raise HTTPException(409, "O projeto ainda não possui relatório para refinar")
    return {"run": start_qa_refinement(project_id)}


@router.get("/runs/{run_id}")
def get_run_status(run_id: str, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    snapshot = _owned_run_or_404(db, user, run_id)
    calls = db.scalars(
        select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.id.asc())
    ).all()
    snapshot["costs"] = _summarize_costs(calls)
    return snapshot


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    snapshot = _owned_run_or_404(db, user, run_id)
    accepted = request_cancel(run_id)
    return {
        "accepted": accepted,
        "run": run_snapshot(run_id),
    }


@router.post("/projects/{project_id}/official-facts", status_code=201)
def add_official_fact(project_id: int, payload: OfficialFactCreate, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    row = OfficialFact(project_id=project_id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.get("/projects/{project_id}/official-facts")
def official_facts(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    rows = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project_id)).all()
    return [
        {
            "id": row.id,
            "label": row.label,
            "value": row.value,
            "source_reference": row.source_reference,
            "page": row.page,
            "evidence": row.evidence,
            "indicator": row.indicator,
            "geography": row.geography,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "unit": row.unit,
        }
        for row in rows
    ]


@router.get("/projects/{project_id}/facts")
def facts(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    return fact_events_for_report(db, project_id)


@router.get("/projects/{project_id}/facts/{event_id}/evidence")
def fact_evidence(project_id: int, event_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    event = db.scalar(select(FactEvent).where(FactEvent.id == event_id, FactEvent.project_id == project_id))
    if not event:
        raise HTTPException(404, "Evento factual não encontrado")
    return [item for item in fact_assertions_for_report(db, project_id) if item["event_id"] == event_id]


@router.post("/projects/{project_id}/plan-searches")
def create_plan(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    return {"created": len(plan_queries(db, project_or_404(db, user, project_id)))}


@router.post("/projects/{project_id}/ai/plan-searches")
def create_ai_plan(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    try:
        with cost_context(project_id=project_id, operation="plan_queries"):
            return {"created": len(plan_queries_with_llm(db, project_or_404(db, user, project_id)))}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/projects/{project_id}/searches")
def searches(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    return [
        {
            "id": row.id,
            "query": row.query,
            "kind": row.kind,
            "purpose": row.purpose,
            "rationale": row.rationale,
            "priority": row.priority,
            "executed_at": row.executed_at,
        }
        for row in db.scalars(select(SearchQuery).where(SearchQuery.project_id == project_id)).all()
    ]


@router.post("/projects/{project_id}/collect")
def collect(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    try:
        return {"added": collect_web(db, project_id)}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/media-items", status_code=201)
def add_manual_item(project_id: int, payload: ManualMediaItemCreate, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    url = str(payload.url)
    canonical = canonicalize(url)
    if db.scalar(select(MediaItem.id).where(MediaItem.project_id == project_id, MediaItem.canonical_url == canonical)):
        raise HTTPException(409, "URL já existe no corpus")
    if payload.query_id is not None:
        query = db.scalar(
            select(SearchQuery).where(
                SearchQuery.id == payload.query_id,
                SearchQuery.project_id == project_id,
            )
        )
        if not query:
            raise HTTPException(422, "query_id não pertence a este projeto")
    row = MediaItem(
        project_id=project_id,
        title=payload.title,
        url=url,
        canonical_url=canonical,
        domain=payload.url.host,
        published_at=payload.published_at,
        snippet=payload.snippet,
        content=payload.content,
        source_name=payload.source_name,
        view_count=payload.view_count,
        query_id=payload.query_id,
        search_source="manual",
        media_origin=classify_media_origin(url, payload.url.host),
        discovery_purposes=[payload.purpose],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.post("/projects/{project_id}/validate-and-classify")
def validate(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    return validate_and_classify(db, project_or_404(db, user, project_id))


@router.post("/projects/{project_id}/ai/classify")
def classify_ai(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    try:
        with cost_context(project_id=project_id, operation="classify"):
            return classify_with_llm(db, project_or_404(db, user, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/projects/{project_id}/metrics")
def get_metrics(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project_or_404(db, user, project_id)
    return metrics(db, project_id)


@router.get("/projects/{project_id}/report")
def report(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    data = metrics(db, project_id)
    dominant = data["themes"][0]["theme"] if data["themes"] else "não identificado"
    profile = project.topic_profile or {}
    if project.has_custom_date_window:
        period_intro = f"Na janela de {project.collection_start} a {project.collection_end}"
    elif profile.get("observed_collection_start") and profile.get("observed_collection_end"):
        period_intro = (
            f"No período observado de {profile['observed_collection_start']} "
            f"a {profile['observed_collection_end']}"
        )
    else:
        period_intro = "Na amostra temática coletada"

    return {
        "title": f"Relatório de Repercussão Midiática — {project.topic}",
        "methodological_note": (
            "A amostra descreve fontes abertas auditáveis. Ausência de item validado não prova ausência de cobertura, "
            "e a camada factual é separada da janela de publicação."
        ),
        "executive_summary": (
            f"{period_intro}, foram localizados {data['items_found']} itens, "
            f"dos quais {data['valid_items']} foram validados em {data['unique_vehicles']} veículos. "
            f"O tema mais frequente foi {dominant}. A camada factual estruturou {data['facts']['events']} evento(s)."
        ),
        "metrics": data,
        "facts": fact_events_for_main_report(db, project_id),
    }


@router.get("/projects/{project_id}/ai/report")
def ai_report(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    try:
        with cost_context(project_id=project_id, operation="draft_report"):
            return draft_report_with_llm(db, project_or_404(db, user, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/qa")
def qa_report(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    payload = cached_report_for_project(db, project_id)
    if not payload:
        raise HTTPException(404, "Relatório ainda não foi gerado")
    with cost_context(project_id=project_id, operation="report_qa"):
        return run_report_qa(db, project, payload)


@router.get("/projects/{project_id}/export.pdf")
def export_pdf(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=False)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    filename = "relatorio-repercussao-midiatica-" + "".join(
        char if char.isalnum() else "-" for char in project.topic.lower()
    ).strip("-") + ".pdf"
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/projects/{project_id}/export-draft.pdf")
def export_draft_pdf(project_id: int, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=True)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    filename = "RASCUNHO-relatorio-repercussao-midiatica-" + "".join(
        char if char.isalnum() else "-" for char in project.topic.lower()
    ).strip("-") + ".pdf"
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

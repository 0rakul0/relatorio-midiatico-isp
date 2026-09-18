from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.cost_tracker import cost_context
from app.database import get_db
from app.fact_layer import fact_assertions_for_report, fact_events_for_main_report, fact_events_for_report
from app.orchestration import request_cancel, run_snapshot, start_run
from app.models import (
    Classification,
    FactAssertion,
    FactEvent,
    GeneratedReport,
    LLMCall,
    MediaItem,
    OfficialFact,
    Project,
    SearchQuery,
)
from app.report_qa import run_report_qa
from app.schema_upgrade import ensure_schema
from app.schemas import ManualMediaItemCreate, OfficialFactCreate, ProjectCreate
from app.services import (
    cached_report_for_project,
    cached_report_for_topic,
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
from app.topic_profile import requested_topic_window


app = FastAPI(title="ISP Repercussão Midiática", version="0.3.1")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup():
    ensure_schema()


def project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Projeto não encontrado")
    return project


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.3.1",
        "search_limits": {
            "max_search_results": settings.max_search_results,
            "max_results_per_query": settings.max_results_per_query,
            "max_search_queries": settings.max_search_queries,
            "duckduckgo_region": settings.duckduckgo_region,
            "duckduckgo_safesearch": settings.duckduckgo_safesearch,
            "duckduckgo_fetch_pages": settings.duckduckgo_fetch_pages,
            "max_youtube_tasks": settings.max_youtube_tasks,
            "max_youtube_results_total": settings.max_youtube_results_total,
            "max_youtube_results_per_task": settings.max_youtube_results_per_task,
            "web_search_provider": "duckduckgo+tavily",
            "youtube_search_provider": "duckduckgo_videos+tavily",
        },
        "agent": {
            "name": "ReportAgent",
            "tool_choice": "model_decides",
            "tools": ["pesquisar_internet", "pesquisar_videos"],
        },
        "ai_limits": {
            "max_semantic_reviews": settings.max_semantic_reviews,
            "validation_batch_size": settings.validation_batch_size,
            "max_classifications": settings.max_classifications,
            "classification_batch_size": settings.classification_batch_size,
            "max_fact_extractions": settings.max_fact_extractions,
            "max_cross_validations": settings.max_cross_validations,
            "validation_item_max_chars": settings.validation_item_max_chars,
            "classification_item_max_chars": settings.classification_item_max_chars,
        },
    }


def _costs_rows(rows) -> list[dict]:
    return [
        {
            "id": row.id,
            "project_id": row.project_id,
            "run_id": row.run_id,
            "operation": row.operation,
            "schema_name": row.schema_name,
            "caller": row.caller,
            "model": row.model,
            "success": row.success,
            "error": row.error,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cached_input_tokens": row.cached_input_tokens,
            "search_calls": row.search_calls,
            "cost_usd": row.cost_usd,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@app.get("/costs")
def costs(db: Session = Depends(get_db), limit: int = 200):
    rows = db.execute(
        select(LLMCall).order_by(LLMCall.id.desc()).limit(max(1, min(limit, 1000)))
    ).scalars().all()
    return {"calls": _costs_rows(rows)}


@app.get("/costs/summary")
def costs_summary(db: Session = Depends(get_db)):
    calls = db.query(LLMCall).all()
    return _summarize_costs(calls)


@app.get("/projects/{project_id}/costs")
def project_costs(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    calls = db.scalars(
        select(LLMCall).where(LLMCall.project_id == project_id).order_by(LLMCall.id.desc())
    ).all()
    return {"project_id": project_id, "calls": _costs_rows(calls), "summary": _summarize_costs(calls)}


def _summarize_costs(calls) -> dict:
    totals = {
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_input_tokens": 0,
        "total_search_calls": 0,
        "calls": len(calls),
        "by_model": {},
        "by_operation": {},
    }
    for call in calls:
        totals["total_cost_usd"] += call.cost_usd or 0.0
        totals["total_input_tokens"] += call.input_tokens or 0
        totals["total_output_tokens"] += call.output_tokens or 0
        totals["total_cached_input_tokens"] += call.cached_input_tokens or 0
        totals["total_search_calls"] += call.search_calls or 0
        model_bucket = totals["by_model"].setdefault(
            call.model, {"model": call.model, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
        )
        _add_to_bucket(model_bucket, call)
        op_key = call.operation or "desconhecida"
        op_bucket = totals["by_operation"].setdefault(
            op_key, {"operation": op_key, "cost_usd": 0.0, "calls": 0}
        )
        op_bucket["cost_usd"] += call.cost_usd or 0.0
        op_bucket["calls"] += 1
    totals["total_cost_usd"] = round(totals["total_cost_usd"], 6)
    return totals


def _add_to_bucket(bucket: dict, call) -> None:
    bucket["cost_usd"] = round(bucket["cost_usd"] + (call.cost_usd or 0.0), 6)
    bucket["input_tokens"] += call.input_tokens or 0
    bucket["output_tokens"] += call.output_tokens or 0
    bucket["calls"] += 1


@app.get("/reports/history")
def report_history(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .order_by(GeneratedReport.generated_at.desc())
        .limit(100)
    ).all()
    history, seen_versions = [], set()
    for project, generated in rows:
        generated_day = generated.generated_at.date().isoformat() if generated.generated_at else "sem-data"
        version_key = (project.topic.strip().casefold(), generated_day)
        if version_key in seen_versions:
            continue
        seen_versions.add(version_key)
        history.append(
            {
                "id": project.id,
                "topic": project.topic,
                "institution": project.institution,
                "project_type": project.project_type,
                "generated_at": generated.generated_at,
                "qa_status": generated.qa_status,
            }
        )
        if len(history) == 20:
            break
    return history


@app.get("/reports/cache")
def cached_report(
    topic: str,
    collection_start: date | None = None,
    collection_end: date | None = None,
    db: Session = Depends(get_db),
):
    report = cached_report_for_topic(db, topic, collection_start, collection_end)
    return {"cached": bool(report), "report": report}


@app.get("/reports/history/{project_id}")
def historical_report(project_id: int, db: Session = Depends(get_db)):
    report = cached_report_for_project(db, project_id)
    if not report:
        raise HTTPException(404, "Versão do relatório não encontrada")
    return {"report": report}


@app.delete("/reports/history/{project_id}", status_code=204)
def delete_historical_report(project_id: int, db: Session = Depends(get_db)):
    target = db.execute(
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(Project.id == project_id)
    ).first()
    if not target:
        raise HTTPException(404, "Versão do relatório não encontrada")

    target_project, target_report = target
    target_day = target_report.generated_at.date() if target_report.generated_at else None
    matching_project_ids = [
        project.id
        for project, generated in db.execute(
            select(Project, GeneratedReport).join(GeneratedReport, GeneratedReport.project_id == Project.id)
        ).all()
        if project.topic.strip().casefold() == target_project.topic.strip().casefold()
        and (generated.generated_at.date() if generated.generated_at else None) == target_day
    ]

    event_ids = select(FactEvent.id).where(FactEvent.project_id.in_(matching_project_ids))
    item_ids = select(MediaItem.id).where(MediaItem.project_id.in_(matching_project_ids))
    db.execute(delete(FactAssertion).where(FactAssertion.event_id.in_(event_ids)))
    db.execute(delete(FactEvent).where(FactEvent.project_id.in_(matching_project_ids)))
    db.execute(delete(Classification).where(Classification.media_item_id.in_(item_ids)))
    db.execute(delete(GeneratedReport).where(GeneratedReport.project_id.in_(matching_project_ids)))
    db.execute(delete(OfficialFact).where(OfficialFact.project_id.in_(matching_project_ids)))
    # MediaItem referencia SearchQuery; remova os itens antes das consultas para
    # funcionar também quando o banco estiver com FKs estritas habilitadas.
    db.execute(delete(MediaItem).where(MediaItem.project_id.in_(matching_project_ids)))
    db.execute(delete(SearchQuery).where(SearchQuery.project_id.in_(matching_project_ids)))
    db.execute(delete(Project).where(Project.id.in_(matching_project_ids)))
    db.commit()
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse("app/static/index.html")


@app.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    today = date.today()
    inferred_window = requested_topic_window(payload.topic)

    if payload.collection_start or payload.collection_end:
        collection_start = payload.collection_start or payload.collection_end
        collection_end = payload.collection_end or payload.collection_start
        has_custom_window = True
    elif inferred_window:
        collection_start, collection_end = inferred_window
        has_custom_window = True
    else:
        collection_start = collection_end = today
        has_custom_window = False

    if payload.event_start or payload.event_end:
        event_start = payload.event_start or payload.event_end
        event_end = payload.event_end or payload.event_start
    elif inferred_window:
        event_start, event_end = inferred_window
    elif has_custom_window:
        event_start, event_end = collection_start, collection_end
    else:
        # Sem datas explícitas, não inventamos uma janela factual de um único dia.
        # O pipeline pesquisará pelo tema e pelas entidades descobertas no perfil.
        event_start = event_end = None

    if collection_end < collection_start:
        raise HTTPException(422, "collection_end deve ser posterior ao início")
    if event_start and event_end and event_end < event_start:
        raise HTTPException(422, "event_end deve ser posterior ao início")

    execution_options = {
        key: value
        for key, value in {
            "enable_youtube": payload.enable_youtube,
            "enable_fact_layer": payload.enable_fact_layer,
            "enable_nominal_followup": payload.enable_nominal_followup,
            "enable_cross_validation": payload.enable_cross_validation,
        }.items()
        if value is not None
    }
    # O campo launch_date do banco continua preenchido por compatibilidade com
    # instalações antigas, mas só deve ser exibido como dado editorial quando
    # o usuário o informou ou quando o agente documentalista o confirmou.
    if payload.launch_date is not None:
        execution_options["launch_date_user_supplied"] = True

    row = Project(
        topic=payload.topic,
        institution=payload.institution,
        launch_date=payload.launch_date or today,
        collection_start=collection_start,
        collection_end=collection_end,
        event_start=event_start,
        event_end=event_end,
        execution_profile=payload.execution_profile,
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
        "discovery": {
            "status": "DEFERRED",
            "message": "O perfil será executado como a primeira etapa acompanhada do relatório.",
        },
    }


@app.post("/projects/{project_id}/discover-profile")
def discover_profile(project_id: int, db: Session = Depends(get_db)):
    try:
        with cost_context(project_id=project_id, operation="discover_profile"):
            return discover_project_profile(db, project_or_404(db, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/projects/{project_id}/run")
def run_project(project_id: int, db: Session = Depends(get_db)):
    try:
        with cost_context(project_id=project_id, operation="run_full_methodology"):
            return run_full_methodology(db, project_or_404(db, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/projects/{project_id}/run-async", status_code=202)
def run_project_async(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    return {"run": start_run(project_id)}


@app.get("/runs/{run_id}")
def get_run_status(run_id: str, db: Session = Depends(get_db)):
    snapshot = run_snapshot(run_id)
    if not snapshot:
        raise HTTPException(404, "Execução não encontrada")
    calls = db.scalars(
        select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.id.asc())
    ).all()
    snapshot["costs"] = _summarize_costs(calls)
    return snapshot


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    snapshot = run_snapshot(run_id)
    if not snapshot:
        raise HTTPException(404, "Execução não encontrada")
    accepted = request_cancel(run_id)
    return {
        "accepted": accepted,
        "run": run_snapshot(run_id),
    }


@app.post("/projects/{project_id}/official-facts", status_code=201)
def add_official_fact(project_id: int, payload: OfficialFactCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    row = OfficialFact(project_id=project_id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@app.get("/projects/{project_id}/official-facts")
def official_facts(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
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


@app.get("/projects/{project_id}/facts")
def facts(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    return fact_events_for_report(db, project_id)


@app.get("/projects/{project_id}/facts/{event_id}/evidence")
def fact_evidence(project_id: int, event_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    event = db.scalar(select(FactEvent).where(FactEvent.id == event_id, FactEvent.project_id == project_id))
    if not event:
        raise HTTPException(404, "Evento factual não encontrado")
    return [item for item in fact_assertions_for_report(db, project_id) if item["event_id"] == event_id]


@app.post("/projects/{project_id}/plan-searches")
def create_plan(project_id: int, db: Session = Depends(get_db)):
    return {"created": len(plan_queries(db, project_or_404(db, project_id)))}


@app.post("/projects/{project_id}/ai/plan-searches")
def create_ai_plan(project_id: int, db: Session = Depends(get_db)):
    try:
        with cost_context(project_id=project_id, operation="plan_queries"):
            return {"created": len(plan_queries_with_llm(db, project_or_404(db, project_id)))}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/projects/{project_id}/searches")
def searches(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
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


@app.post("/projects/{project_id}/collect")
def collect(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    try:
        return {"added": collect_web(db, project_id)}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/projects/{project_id}/media-items", status_code=201)
def add_manual_item(project_id: int, payload: ManualMediaItemCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
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
        query_id=payload.query_id,
        search_source="manual",
        discovery_purposes=[payload.purpose],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@app.post("/projects/{project_id}/validate-and-classify")
def validate(project_id: int, db: Session = Depends(get_db)):
    return validate_and_classify(db, project_or_404(db, project_id))


@app.post("/projects/{project_id}/ai/classify")
def classify_ai(project_id: int, db: Session = Depends(get_db)):
    try:
        with cost_context(project_id=project_id, operation="classify"):
            return classify_with_llm(db, project_or_404(db, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/projects/{project_id}/metrics")
def get_metrics(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    return metrics(db, project_id)


@app.get("/projects/{project_id}/report")
def report(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
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


@app.get("/projects/{project_id}/ai/report")
def ai_report(project_id: int, db: Session = Depends(get_db)):
    try:
        with cost_context(project_id=project_id, operation="draft_report"):
            return draft_report_with_llm(db, project_or_404(db, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/projects/{project_id}/qa")
def qa_report(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    payload = cached_report_for_project(db, project_id)
    if not payload:
        raise HTTPException(404, "Relatório ainda não foi gerado")
    with cost_context(project_id=project_id, operation="report_qa"):
        return run_report_qa(db, project, payload)


@app.get("/projects/{project_id}/export.pdf")
def export_pdf(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=False)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    filename = "relatorio-repercussao-midiatica-" + "".join(
        char if char.isalnum() else "-" for char in project.topic.lower()
    ).strip("-") + ".pdf"
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/projects/{project_id}/export-draft.pdf")
def export_draft_pdf(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=True)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    filename = "RASCUNHO-relatorio-repercussao-midiatica-" + "".join(
        char if char.isalnum() else "-" for char in project.topic.lower()
    ).strip("-") + ".pdf"
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

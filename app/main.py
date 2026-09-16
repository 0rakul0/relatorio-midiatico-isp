from datetime import date
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session
from app.database import Base, engine, get_db
from app.config import get_settings
from app.models import Classification, GeneratedReport, MediaItem, OfficialFact, Project, SearchQuery
from app.schemas import LLMSettingsUpdate, ManualMediaItemCreate, OfficialFactCreate, ProjectCreate
from app.services import cached_report_for_project, cached_report_for_topic, canonicalize, classify_with_llm, collect_tavily, discover_project_profile, draft_report_with_llm, export_report_pdf, metrics, plan_queries, plan_queries_with_llm, requested_month_window, run_full_methodology, validate_and_classify

app = FastAPI(title="ISP Repercussão Midiática", version="0.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    # Migração leve para bancos SQLite/PostgreSQL já existentes, sem apagar o corpus.
    columns = {column["name"] for column in inspect(engine).get_columns("media_items")}
    with engine.begin() as connection:
        if "source_name" not in columns:
            connection.execute(text("ALTER TABLE media_items ADD COLUMN source_name VARCHAR(300)"))
        if "view_count" not in columns:
            connection.execute(text("ALTER TABLE media_items ADD COLUMN view_count INTEGER"))
    project_columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    if "has_custom_date_window" not in project_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE projects ADD COLUMN has_custom_date_window BOOLEAN DEFAULT 0"))


def project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "Projeto não encontrado")
    return project


@app.get("/health")
def health(): return {"status": "ok"}


@app.get("/settings/llm")
def get_llm_settings():
    settings = get_settings()
    return {"provider": settings.llm_provider, "model": settings.groq_model if settings.llm_provider == "groq" else settings.openai_model}


@app.post("/settings/llm")
def update_llm_settings(payload: LLMSettingsUpdate):
    settings = get_settings()
    if payload.provider == "groq" and not settings.groq_api_key:
        raise HTTPException(422, "GROQ_API_KEY não configurada no servidor")
    if payload.provider == "openai" and not settings.openai_api_key:
        raise HTTPException(422, "OPENAI_API_KEY não configurada no servidor")
    settings.llm_provider = payload.provider
    if payload.provider == "groq": settings.groq_model = payload.model
    else: settings.openai_model = payload.model
    return {"provider": payload.provider, "model": payload.model}


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
        history.append({
            "id": project.id, "topic": project.topic, "institution": project.institution,
            "launch_date": str(project.launch_date), "generated_at": generated.generated_at,
        })
        if len(history) == 20:
            break
    return history


@app.get("/reports/cache")
def cached_report(topic: str, collection_start: date | None = None, collection_end: date | None = None, db: Session = Depends(get_db)):
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
    """Remove o item exibido no histórico, incluindo revisões do mesmo tema e dia."""
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
        project.id for project, generated in db.execute(
            select(Project, GeneratedReport).join(GeneratedReport, GeneratedReport.project_id == Project.id)
        ).all()
        if project.topic.strip().casefold() == target_project.topic.strip().casefold()
        and (generated.generated_at.date() if generated.generated_at else None) == target_day
    ]
    item_ids = select(MediaItem.id).where(MediaItem.project_id.in_(matching_project_ids))
    db.execute(delete(Classification).where(Classification.media_item_id.in_(item_ids)))
    db.execute(delete(GeneratedReport).where(GeneratedReport.project_id.in_(matching_project_ids)))
    db.execute(delete(OfficialFact).where(OfficialFact.project_id.in_(matching_project_ids)))
    db.execute(delete(SearchQuery).where(SearchQuery.project_id.in_(matching_project_ids)))
    db.execute(delete(MediaItem).where(MediaItem.project_id.in_(matching_project_ids)))
    db.execute(delete(Project).where(Project.id.in_(matching_project_ids)))
    db.commit()
    return Response(status_code=204)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse("app/static/index.html")


@app.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    today = date.today()
    has_custom_window = bool(payload.collection_start or payload.collection_end)
    if has_custom_window:
        start = payload.collection_start or payload.collection_end
        end = payload.collection_end or payload.collection_start
    else:
        inferred_window = requested_month_window(payload.topic)
        if inferred_window:
            start, end = inferred_window
            # A data explícita no tema é uma instrução do solicitante e deve
            # permanecer intacta após a descoberta do perfil documental.
            has_custom_window = True
        else:
            start = end = today
    launch = payload.launch_date or today
    if end < start: raise HTTPException(422, "collection_end deve ser posterior ao início")
    row = Project(topic=payload.topic, institution=payload.institution, launch_date=launch, collection_start=start, collection_end=end,
                  has_custom_date_window=has_custom_window, status="CUSTOM_DATES" if has_custom_window else "DRAFT")
    db.add(row); db.commit(); db.refresh(row)
    try:
        discovery = discover_project_profile(db, row)
    except RuntimeError as exc:
        discovery = {"status": "PROFILE_NEEDS_REVIEW", "warning": str(exc)}
    return {"id": row.id, "status": row.status, "discovery": discovery}


@app.post("/projects/{project_id}/discover-profile")
def discover_profile(project_id: int, db: Session = Depends(get_db)):
    try: return discover_project_profile(db, project_or_404(db, project_id))
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.post("/projects/{project_id}/run")
def run_project(project_id: int, db: Session = Depends(get_db)):
    try: return run_full_methodology(db, project_or_404(db, project_id))
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.post("/projects/{project_id}/official-facts", status_code=201)
def add_official_fact(project_id: int, payload: OfficialFactCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id); row = OfficialFact(project_id=project_id, **payload.model_dump()); db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id}


@app.get("/projects/{project_id}/official-facts")
def official_facts(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    rows = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project_id)).all()
    return [{"id": row.id, "label": row.label, "value": row.value, "source_reference": row.source_reference,
             "page": row.page, "evidence": row.evidence} for row in rows]


@app.post("/projects/{project_id}/plan-searches")
def create_plan(project_id: int, db: Session = Depends(get_db)):
    return {"created": len(plan_queries(db, project_or_404(db, project_id)))}


@app.post("/projects/{project_id}/ai/plan-searches")
def create_ai_plan(project_id: int, db: Session = Depends(get_db)):
    try: return {"created": len(plan_queries_with_llm(db, project_or_404(db, project_id)))}
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.get("/projects/{project_id}/searches")
def searches(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    return [{"id": x.id, "query": x.query, "kind": x.kind, "rationale": x.rationale, "executed_at": x.executed_at} for x in db.scalars(select(SearchQuery).where(SearchQuery.project_id == project_id)).all()]


@app.post("/projects/{project_id}/collect")
def collect(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id)
    try: return {"added": collect_tavily(db, project_id)}
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.post("/projects/{project_id}/media-items", status_code=201)
def add_manual_item(project_id: int, payload: ManualMediaItemCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id); url = str(payload.url); canonical = canonicalize(url)
    if db.scalar(select(MediaItem.id).where(MediaItem.project_id == project_id, MediaItem.canonical_url == canonical)):
        raise HTTPException(409, "URL já existe no corpus")
    row = MediaItem(project_id=project_id, title=payload.title, url=url, canonical_url=canonical, domain=payload.url.host,
        published_at=payload.published_at, snippet=payload.snippet, content=payload.content, query_id=payload.query_id, search_source="manual")
    db.add(row); db.commit(); return {"id": row.id}


@app.post("/projects/{project_id}/validate-and-classify")
def validate(project_id: int, db: Session = Depends(get_db)):
    return validate_and_classify(db, project_or_404(db, project_id))


@app.post("/projects/{project_id}/ai/classify")
def classify_ai(project_id: int, db: Session = Depends(get_db)):
    try: return classify_with_llm(db, project_or_404(db, project_id))
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.get("/projects/{project_id}/metrics")
def get_metrics(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id); return metrics(db, project_id)


@app.get("/projects/{project_id}/report")
def report(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id); data = metrics(db, project_id)
    dominant = data["themes"][0]["theme"] if data["themes"] else "não identificado"
    return {"title": f"Relatório de Repercussão Midiática — {project.topic}", "methodological_note": "A amostra descreve fontes abertas auditáveis; não mede audiência ou alcance e ausência de resultado não prova ausência de cobertura.",
            "executive_summary": f"Na janela de {project.collection_start} a {project.collection_end}, foram localizados {data['items_found']} itens, dos quais {data['valid_items']} foram validados em {data['unique_vehicles']} veículos. O tema mais frequente foi {dominant}. O ISP foi identificado como fonte em {data['isp_protagonism_percent']}% dos itens validados.", "metrics": data}


@app.get("/projects/{project_id}/ai/report")
def ai_report(project_id: int, db: Session = Depends(get_db)):
    try: return draft_report_with_llm(db, project_or_404(db, project_id))
    except RuntimeError as exc: raise HTTPException(503, str(exc))


@app.get("/projects/{project_id}/export.pdf")
def export_pdf(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id)
    try:
        content = export_report_pdf(db, project)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    filename = "relatorio-repercussao-midiatica-" + "".join(char if char.isalnum() else "-" for char in project.topic.lower()).strip("-") + ".pdf"
    return Response(content=content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

from datetime import date
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import Base, engine, get_db
from app.models import MediaItem, OfficialFact, Project, SearchQuery
from app.schemas import ManualMediaItemCreate, OfficialFactCreate, ProjectCreate
from app.services import canonicalize, collect_tavily, metrics, plan_queries, validate_and_classify

app = FastAPI(title="ISP Repercussão Midiática", version="0.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)


def project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project: raise HTTPException(404, "Projeto não encontrado")
    return project


@app.get("/health")
def health(): return {"status": "ok"}


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse("app/static/index.html")


@app.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    if payload.collection_end < payload.collection_start: raise HTTPException(422, "collection_end deve ser posterior ao início")
    row = Project(**payload.model_dump()); db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status}


@app.post("/projects/{project_id}/official-facts", status_code=201)
def add_official_fact(project_id: int, payload: OfficialFactCreate, db: Session = Depends(get_db)):
    project_or_404(db, project_id); row = OfficialFact(project_id=project_id, **payload.model_dump()); db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id}


@app.post("/projects/{project_id}/plan-searches")
def create_plan(project_id: int, db: Session = Depends(get_db)):
    return {"created": len(plan_queries(db, project_or_404(db, project_id)))}


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


@app.get("/projects/{project_id}/metrics")
def get_metrics(project_id: int, db: Session = Depends(get_db)):
    project_or_404(db, project_id); return metrics(db, project_id)


@app.get("/projects/{project_id}/report")
def report(project_id: int, db: Session = Depends(get_db)):
    project = project_or_404(db, project_id); data = metrics(db, project_id)
    dominant = data["themes"][0]["theme"] if data["themes"] else "não identificado"
    return {"title": f"Relatório de Repercussão Midiática — {project.topic}", "methodological_note": "A amostra descreve fontes abertas auditáveis; não mede audiência ou alcance e ausência de resultado não prova ausência de cobertura.",
            "executive_summary": f"Na janela de {project.collection_start} a {project.collection_end}, foram localizados {data['items_found']} itens, dos quais {data['valid_items']} foram validados em {data['unique_vehicles']} veículos. O tema mais frequente foi {dominant}. O ISP foi identificado como fonte em {data['isp_protagonism_percent']}% dos itens validados.", "metrics": data}

from collections import Counter
from datetime import date
from contextlib import asynccontextmanager
import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.auth import AuthUser, get_current_user
from app.api.deps import project_or_404
from app.api.chat import router as chat_router
from app.api.costs import router as costs_router, summarize_costs as _summarize_costs
from app.api.reports import router as reports_router
from app.api.projects import router as projects_router
from app.billing import check_quota, clamp_profile, plan_for, router as billing_router
from app.cost_tracker import cost_context
from app.database import SessionLocal, get_db
from app.fact_layer import fact_assertions_for_report, fact_events_for_main_report, fact_events_for_report
from app.orchestration import request_cancel, resume_run, run_snapshot, start_qa_refinement, start_run
from app.models import (
    AcademicPaper,
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
from app.schemas import ChatAskRequest, ManualMediaItemCreate, OfficialFactCreate, ProjectCreate
from app.services import (
    cached_report_for_project,
    cached_report_for_topic,
    canonicalize,
    chat_with_all_corpus,
    chat_with_corpus,
    delete_chat_conversation,
    get_chat_conversation,
    list_chat_conversations,
    persist_chat_exchange,
    classify_with_llm,
    collect_web,
    discover_project_profile,
    draft_report_with_llm,
    export_report_pdf,
    list_chat_projects,
    metrics,
    plan_queries,
    plan_queries_with_llm,
    run_full_methodology,
    validate_and_classify,
)
from app.services.collection.media_origin import classify_media_origin
from app.services.corpus_metadata import repair_corpus_metadata
from app.topic_profile import requested_topic_window


logger = logging.getLogger("app.main")


BRAZILIAN_STATES = frozenset({
    "Acre", "Alagoas", "Amapá", "Amazonas", "Bahia", "Ceará", "Distrito Federal",
    "Espírito Santo", "Goiás", "Maranhão", "Mato Grosso", "Mato Grosso do Sul",
    "Minas Gerais", "Pará", "Paraíba", "Paraná", "Pernambuco", "Piauí",
    "Rio de Janeiro", "Rio Grande do Norte", "Rio Grande do Sul", "Rondônia",
    "Roraima", "Santa Catarina", "São Paulo", "Sergipe", "Tocantins",
})


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_schema()

    settings = get_settings()
    if settings.corpus_metadata_repair_on_startup:
        try:
            with SessionLocal() as db:
                stats = repair_corpus_metadata(
                    db,
                    limit=settings.corpus_metadata_repair_limit,
                )
                db.commit()
                logger.info("Corpus metadata repair: %s", stats)
        except Exception:
            # A limpeza do acervo nao deve impedir a aplicacao de iniciar.
            logger.exception("Falha ao normalizar metadados do corpus")

    yield


app = FastAPI(title="ISP Repercussão Midiática", version="0.3.4", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(billing_router)
app.include_router(costs_router)
app.include_router(chat_router)
app.include_router(reports_router)
app.include_router(projects_router)


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "version": "0.3.4",
        "features": {"qa_history_refinement": True, "qa_refinement_paths": ["/reports/history/{project_id}/refine", "/projects/{project_id}/refine-qa-async"]},
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
            "web_search_provider": "duckduckgo",
            "youtube_search_provider": "duckduckgo_videos",
        },
        "agent": {
            "name": "ReportAgent",
            "tool_choice": "model_decides",
            "tools": ["pesquisar_internet", "pesquisar_videos", "pesquisar_artigos_arxiv"],
        },
        "ai_limits": {
            "max_semantic_reviews": settings.max_semantic_reviews,
            "validation_batch_size": settings.validation_batch_size,
            "max_classifications": settings.max_classifications,
            "classification_batch_size": settings.classification_batch_size,
            "max_fact_extractions": settings.max_fact_extractions,
            "validation_item_max_chars": settings.validation_item_max_chars,
            "classification_item_max_chars": settings.classification_item_max_chars,
        },
    }


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse("app/static/index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse("app/static/login.html")


@app.get("/auth/config", include_in_schema=False)
def auth_config():
    # Chave publishable é pública por desenho (vai para o browser).
    settings = get_settings()
    return {
        "supabase_url": settings.supabase_url,
        "supabase_key": settings.supabase_key,
        "local_auth_bypass": settings.local_auth_bypass,
    }

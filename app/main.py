from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.chat import router as chat_router
from app.api.institutional_presence import router as institutional_router
from app.api.costs import router as costs_router
from app.api.projects import router as projects_router
from app.api.project_search import router as project_search_router
from app.api.project_collection import router as project_collection_router
from app.api.runs import router as runs_router
from app.api.facts import router as facts_router
from app.api.project_reports import router as project_reports_router
from app.api.reports import router as reports_router
from app.auth import router as auth_router, get_current_user
from app.billing import router as billing_router
from app.config import get_settings
from app.database import SessionLocal
from app.orchestration.scheduler import RunQueueFull
from app.orchestration.state import recover_orphaned_runs
from app.schema_upgrade import ensure_schema
from app.services.corpus_metadata import repair_corpus_metadata


logger = logging.getLogger("app.main")



def validate_startup_configuration(settings) -> None:
    """Falhar antes de servir login se a assinatura de sessoes nao estiver pronta."""
    if settings.app_env.casefold() == "production" and settings.local_auth_bypass:
        raise RuntimeError("LOCAL_AUTH_BYPASS nao pode ser usado em producao")
    # A mesma regra vale em desenvolvimento quando o bypass esta desligado:
    # nao deixar a API aceitar requisicoes que responderiam 503 ao login.
    if not settings.local_auth_bypass and len(
        (settings.local_session_secret or "").strip().encode("utf-8")
    ) < 32:
        raise RuntimeError(
            "Autenticacao local indisponivel: configure LOCAL_SESSION_SECRET "
            "com pelo menos 32 caracteres aleatorios no .env. "
            "Gere uma chave persistente com: openssl rand -hex 32"
        )
    if settings.openai_only and settings.app_env.casefold() == "production":
        if not (settings.openai_api_key or "").strip():
            raise RuntimeError("OPENAI_ONLY=true: configure OPENAI_API_KEY no .env")
        if (settings.openai_base_url or "").strip():
            raise RuntimeError("OPENAI_ONLY=true: remova OPENAI_BASE_URL para usar a API oficial")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    validate_startup_configuration(settings)
    ensure_schema()
    if settings.app_env.casefold() == 'production':
        recovered = recover_orphaned_runs()
        if recovered:
            logger.warning('Recuperadas %s execucoes interrompidas de processo anterior', recovered)
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


@app.exception_handler(RunQueueFull)
async def run_queue_full_handler(request, exc: RunQueueFull):
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
        headers={"Retry-After": "10"},
    )
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(costs_router)
app.include_router(chat_router)
app.include_router(institutional_router)
app.include_router(reports_router)
app.include_router(projects_router)
app.include_router(project_search_router)
app.include_router(project_collection_router)
app.include_router(runs_router)
app.include_router(facts_router)
app.include_router(project_reports_router)


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
        "parallel_reports": {
            "max_active": settings.max_concurrent_reports,
            "max_waiting": settings.max_queued_reports,
            "sqlite_mode": "WAL",
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
    # Compatibilidade para clientes antigos; nao expoe segredos.
    settings = get_settings()
    return {
        "provider": "local",
        "local_auth_bypass": settings.local_auth_bypass,
        "signup_enabled": bool(settings.local_signup_enabled and not settings.local_auth_bypass),
    }

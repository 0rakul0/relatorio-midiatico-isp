from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project
from app.services.collection.orchestrator import run_agent_collection, web_queries_pending
from app.tools.search import search_providers_available


def new_web_counters(global_limit: int) -> dict[str, int]:
    return {
        "queries_total": 0,
        "queries_attempted": 0,
        "queries_successful": 0,
        "duckduckgo_queries": 0,
        "duckduckgo_results": 0,
        "duckduckgo_added": 0,
        "duckduckgo_rejected": 0,
        "duckduckgo_zero_usable": 0,
        "tavily_attempts": 0,
        "tavily_queries": 0,
        "tavily_results": 0,
        "tavily_added": 0,
        "tavily_rejected": 0,
        "tavily_zero_usable": 0,
        "tavily_hard_failures": 0,
        "tavily_circuit_breaker_trips": 0,
        "failed_queries": 0,
        "global_result_limit": global_limit,
        "global_limit_reached": 0,
    }


def collect_web(
    db: Session,
    project_id: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    """Coleta web obrigatória executada pelo agente.

    O serviço não acessa provedores: ele informa a metodologia (consultas já
    planejadas) e o agente executa cada consulta via ``pesquisar_internet``,
    persistindo pelos sinks do orquestrador.
    """
    settings = get_settings()
    if not search_providers_available():
        raise RuntimeError(
            "Nenhum provedor de pesquisa está disponível. Instale `ddgs` "
            "ou configure TAVILY_API_KEY."
        )

    project = db.get(Project, project_id)
    if not project:
        raise RuntimeError("Projeto nao encontrado")

    queries = web_queries_pending(project_id)
    counters = new_web_counters(max(1, settings.max_search_results))
    state, _video = run_agent_collection(
        project_id=project_id,
        web_queries=queries,
        web_counters=counters,
        web_progress=progress_detail,
        cancel_check=cancel_check,
    )

    if state.cancelled and state.cancel_exc is not None:
        raise state.cancel_exc
    if state.unavailable:
        raise RuntimeError(state.unavailable)

    if stats is not None:
        stats.clear()
        stats.update(counters)

    if (
        queries
        and int(counters.get("queries_successful", 0)) == 0
        and int(counters.get("failed_queries", 0)) > 0
    ):
        detail = " | ".join(state.errors[:3])[:1800]
        raise RuntimeError("Coleta web indisponivel. " + detail)

    return state.added

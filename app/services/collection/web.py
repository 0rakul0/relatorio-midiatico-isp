from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project
from app.services.collection.orchestrator import run_agent_collection, web_queries_pending
from app.tools.search import search_providers_available


def new_web_counters(global_limit: int, target_media_items: int | None = None) -> dict[str, int]:
    settings = get_settings()
    target = int(target_media_items or settings.target_media_items)
    return {
        "queries_total": 0,
        "queries_attempted": 0,
        "queries_successful": 0,
        "duckduckgo_queries": 0,
        "duckduckgo_results": 0,
        "duckduckgo_added": 0,
        "duckduckgo_rejected": 0,
        "duckduckgo_zero_usable": 0,
        "failed_queries": 0,
        # Compatibilidade: o valor continua exposto nas estatisticas, mas a
        # coleta nao interrompe resultados por esse teto. O volume real e
        # controlado pelo numero de consultas e pelo limite por consulta.
        "global_result_limit": int(global_limit),
        "global_limit_reached": 0,
        "raw_hits": 0,
        "flagged_hits": 0,
        "invalid_hits": 0,
        "target_media_items": target,
        "media_target_reached": 0,
        "media_added": 0,
        "fact_added": 0,
        "official_added": 0,
        "nominal_added": 0,
        "other_added": 0,
    }


def collect_web(
    db: Session,
    project_id: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    """Execute the approved web plan through ReportAgent bulk tools."""
    settings = get_settings()
    if not search_providers_available():
        raise RuntimeError(
            "Nenhum provedor de pesquisa esta disponivel. Instale ddgs."
        )

    project = db.get(Project, project_id)
    if not project:
        raise RuntimeError("Projeto nao encontrado")

    queries = web_queries_pending(project_id)
    counters = new_web_counters(
        max(1, settings.max_search_results),
        target_media_items=settings.target_media_items,
    )
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

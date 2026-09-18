from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project
from app.services.collection.orchestrator import (
    CollectionState,
    run_agent_collection,
    video_queries_pending,
    web_queries_pending,
)
from app.services.collection.web import new_web_counters


def _new_video_counters() -> dict[str, int]:
    return {
        "tasks_total": 0,
        "tasks_resolved": 0,
        "tasks_failed": 0,
        "duckduckgo_attempts": 0,
        "duckduckgo_added": 0,
        "tavily_attempts": 0,
        "tavily_added": 0,
    }


def _providers_label(*flags: tuple[str, bool]) -> str | None:
    providers = [name for name, used in flags if used]
    return "+".join(providers) if providers else None


def _web_result(state: CollectionState) -> dict[str, object]:
    counters = state.counters
    if state.unavailable:
        return {
            "status": "UNAVAILABLE",
            "collected": 0,
            "error": state.unavailable,
            "provider": None,
            "stats": counters,
        }
    if state.expect_queries and not state.invoked:
        return {
            "status": "UNAVAILABLE",
            "collected": 0,
            "error": "O agente de coleta não executou a coleta web planejada",
            "provider": None,
            "stats": counters,
        }
    failed = int(counters.get("failed_queries", 0))
    return {
        "status": "PARTIAL" if failed else "COMPLETED",
        "collected": state.added,
        "error": None,
        "provider": _providers_label(
            ("duckduckgo", int(counters.get("duckduckgo_queries", 0)) > 0),
            ("tavily", int(counters.get("tavily_queries", 0)) > 0),
        ),
        "stats": counters,
    }


def _video_result(state: CollectionState | None) -> dict[str, object]:
    if state is None:
        return {"status": "DISABLED", "collected": 0, "error": None, "provider": None, "stats": {}}
    counters = state.counters
    if state.unavailable:
        return {
            "status": "UNAVAILABLE",
            "collected": 0,
            "error": state.unavailable,
            "provider": None,
            "stats": counters,
        }
    if state.expect_queries and not state.invoked:
        return {
            "status": "UNAVAILABLE",
            "collected": 0,
            "error": "O agente de coleta não executou a coleta de vídeos planejada",
            "provider": None,
            "stats": counters,
        }
    failed = int(counters.get("tasks_failed", 0))
    return {
        "status": "PARTIAL" if failed else "COMPLETED",
        "collected": state.added,
        "error": None,
        "provider": _providers_label(
            ("duckduckgo_video", int(counters.get("duckduckgo_attempts", 0)) > 0),
            ("tavily", int(counters.get("tavily_attempts", 0)) > 0),
        ),
        "stats": counters,
    }


def collect_media_sources(
    db: Session,
    project: Project,
    *,
    enable_youtube: bool = True,
    cancel_check: Callable[[], None] | None = None,
    web_progress: Callable[[str], None] | None = None,
    youtube_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Executa a coleta obrigatória de web (e vídeo opcional) pelo agente.

    O serviço informa a metodologia (consultas web já planejadas e tarefas de
    vídeo) e o agente executa o plano via ``pesquisar_internet`` /
    ``pesquisar_videos``. Nenhum provedor é acessado aqui.
    """
    settings = get_settings()
    web_queries = web_queries_pending(project.id)
    web_counters = new_web_counters(max(1, settings.max_search_results))

    video_queries: list[str] | None = None
    video_counters: dict[str, int] | None = None
    if enable_youtube:
        video_queries = video_queries_pending(project.id)
        video_counters = _new_video_counters()

    web_state, video_state = run_agent_collection(
        project_id=project.id,
        web_queries=web_queries,
        web_counters=web_counters,
        web_progress=web_progress,
        video_queries=video_queries,
        video_counters=video_counters,
        video_progress=youtube_progress,
        cancel_check=cancel_check,
    )

    if cancel_check and (web_state.cancelled or (video_state is not None and video_state.cancelled)):
        exc = web_state.cancel_exc or (video_state.cancel_exc if video_state else None)
        if exc is not None:
            raise exc

    return {
        "web": _web_result(web_state),
        "youtube": _video_result(video_state if enable_youtube else None),
        "parallel": False,
    }

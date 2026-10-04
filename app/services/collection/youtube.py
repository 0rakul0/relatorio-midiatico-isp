from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MediaItem, Project
from app.services.collection.media_origin import YOUTUBE
from app.services.collection.orchestrator import CollectionState, run_agent_collection, web_queries_pending
from app.services.collection.web import new_web_counters


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
            "error": "O agente de coleta nao executou a coleta web planejada",
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
        ),
        "stats": counters,
    }


def _youtube_result(db: Session, project_id: int) -> dict[str, object]:
    """Resume URLs do YouTube descobertas pela coleta principal do DuckDuckGo.

    Nao existe uma segunda descoberta via DuckDuckGo Videos. O YouTube e uma
    rota derivada dos resultados da busca web principal, identificada por
    media_origin=YOUTUBE.
    """
    count = int(
        db.scalar(
            select(func.count(MediaItem.id)).where(
                MediaItem.project_id == project_id,
                MediaItem.media_origin == YOUTUBE,
            )
        )
        or 0
    )
    return {
        "status": "ROUTED",
        "collected": count,
        "error": None,
        "provider": "duckduckgo",
        "stats": {"routed_from_web": count},
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
    """Executa uma unica camada de descoberta externa: DuckDuckGo Web.

    Os resultados sao persistidos e classificados por origem. URLs do YouTube
    sao contabilizadas como rota especializada, sem abrir uma segunda busca.
    Os parametros de YouTube permanecem na assinatura apenas por compatibilidade
    com chamadas existentes.
    """
    settings = get_settings()
    web_queries = web_queries_pending(project.id)
    web_counters = new_web_counters(max(1, settings.max_search_results))

    web_state, _video_state = run_agent_collection(
        project_id=project.id,
        web_queries=web_queries,
        web_counters=web_counters,
        web_progress=web_progress,
        video_queries=None,
        video_counters=None,
        video_progress=None,
        cancel_check=cancel_check,
    )

    if cancel_check and web_state.cancelled and web_state.cancel_exc is not None:
        raise web_state.cancel_exc

    db.expire_all()
    youtube = _youtube_result(db, project.id)
    if youtube_progress:
        youtube_progress(
            f"{youtube['collected']} URL(s) do YouTube roteada(s) a partir da descoberta DuckDuckGo"
        )

    return {
        "web": _web_result(web_state),
        "youtube": youtube,
        "parallel": False,
    }

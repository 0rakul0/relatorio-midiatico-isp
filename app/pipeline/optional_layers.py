from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import Project
from app.orchestration.state import RunCancelled
from app.services.public_opinion import collect_public_opinion
from app.services.social_repercussion import collect_social_repercussion


StageCallback = Callable[[str, str, str | None], None]


def run_social_layer(
    db: Session,
    project: Project,
    *,
    stage: StageCallback,
    check: Callable[[], None],
) -> dict:
    social_repercussion = {
        "status": "SKIPPED",
        "posts": 0,
        "comments": 0,
        "analyzed_comments": 0,
        "methodology_note": (
            "Comentarios em redes sociais descrevem apenas a amostra observada "
            "e nao representam a populacao."
        ),
    }
    check()
    stage(
        "social_repercussion",
        "RUNNING",
        "Buscando posts diretamente por plataforma no DuckDuckGo e enriquecendo comentarios via Apify",
    )
    try:
        social_repercussion = collect_social_repercussion(db, project)
    except RuntimeError as exc:
        if isinstance(exc, RunCancelled):
            raise
        social_repercussion = {
            "status": "UNAVAILABLE",
            "reason": str(exc)[:500],
            "posts": 0,
            "comments": 0,
            "analyzed_comments": 0,
        }
        stage(
            "social_repercussion",
            "SKIPPED",
            f"Repercussao social indisponivel: {str(exc)[:180]}",
        )
        return social_repercussion

    social_status = str(social_repercussion.get("status") or "")
    if social_status in {"DISABLED", "NOT_CONFIGURED", "NO_POSTS"}:
        discovery = social_repercussion.get("discovery") or {}
        platform_audit = discovery.get("platforms") or {}
        platform_text = ", ".join(
            f"{name}: {int((data or {}).get('returned') or 0)}"
            for name, data in platform_audit.items()
        )
        detail = str(
            social_repercussion.get("reason")
            or "Nenhum comentario social disponivel na amostra"
        )
        if discovery:
            detail += (
                f" | {discovery.get('queries', 0)} consulta(s), "
                f"{discovery.get('rounds', 0)} rodada(s), "
                f"{discovery.get('returned', 0)} retorno(s)"
            )
            if platform_text:
                detail += f" | {platform_text}"
        stage("social_repercussion", "SKIPPED", detail[:500])
        return social_repercussion

    discovery = social_repercussion.get("discovery") or {}
    discovery_note = (
        f"; descoberta dedicada: {discovery.get('eligible_posts', 0)} post(s) elegivel(is), "
        f"{discovery.get('new_items', 0)} novo(s), "
        f"{discovery.get('queries', 0)} consulta(s), "
        f"{discovery.get('rounds', 1)} rodada(s)"
        if discovery
        else ""
    )
    stage(
        "social_repercussion",
        "DONE",
        f"{social_repercussion.get('posts', 0)} post(s); "
        f"{social_repercussion.get('comments', 0)} comentario(s); "
        f"{social_repercussion.get('analyzed_comments', 0)} analisado(s)"
        + discovery_note,
    )
    return social_repercussion


def run_public_opinion_layer(
    db: Session,
    project: Project,
    *,
    stage: StageCallback,
    check: Callable[[], None],
) -> dict:
    public_opinion = {
        "status": "NO_SURVEYS",
        "count": 0,
        "surveys": [],
    }
    check()
    stage(
        "public_opinion",
        "RUNNING",
        "Buscando pesquisas de opinião com população, amostra, período de campo e metodologia auditáveis",
    )
    try:
        public_opinion = collect_public_opinion(db, project)
    except RuntimeError as exc:
        public_opinion = {
            "status": "UNAVAILABLE",
            "count": 0,
            "surveys": [],
            "reason": str(exc)[:500],
        }
        stage(
            "public_opinion",
            "SKIPPED",
            f"Camada de opinião pública indisponível: {str(exc)[:180]}",
        )
        return public_opinion

    opinion_status = str(public_opinion.get("status") or "")
    if opinion_status == "DISABLED":
        stage(
            "public_opinion",
            "SKIPPED",
            "Camada de opinião pública desativada na configuração",
        )
    elif opinion_status in {"NO_SURVEYS", "DISCOVERED_ONLY"}:
        detail = (
            f"Busca executada: {public_opinion.get('candidates', 0)} fonte(s) candidata(s); "
            f"{public_opinion.get('count', 0)} pesquisa(s) estruturada(s)"
        )
        if opinion_status == "DISCOVERED_ONLY":
            detail += "; LLM indisponível para estruturar a metodologia"
        stage("public_opinion", "DONE", detail)
    else:
        stage(
            "public_opinion",
            "DONE",
            f"{public_opinion.get('count', 0)} pesquisa(s) de opinião estruturada(s) "
            f"a partir de {public_opinion.get('candidates', 0)} fonte(s) candidata(s)",
        )
    return public_opinion

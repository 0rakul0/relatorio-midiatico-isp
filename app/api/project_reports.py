from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.cost_tracker import cost_context
from app.database import get_db
from app.fact_layer import fact_events_for_main_report
from app.report_qa import run_report_qa
from app.services import (
    cached_report_for_project,
    draft_report_with_llm,
    export_report_pdf,
    metrics,
)


router = APIRouter(tags=["project-reports"])


@router.get("/projects/{project_id}/report")
def report(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
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


@router.get("/projects/{project_id}/ai/report")
def ai_report(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    try:
        with cost_context(project_id=project_id, operation="draft_report"):
            return draft_report_with_llm(db, project_or_404(db, user, project_id))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/qa")
def qa_report(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    payload = cached_report_for_project(db, project_id)
    if not payload:
        raise HTTPException(404, "Relatório ainda não foi gerado")
    with cost_context(project_id=project_id, operation="report_qa"):
        return run_report_qa(db, project, payload)


def _pdf_filename(topic: str, *, draft: bool) -> str:
    prefix = "RASCUNHO-" if draft else ""
    slug = "".join(
        char if char.isalnum() else "-"
        for char in topic.lower()
    ).strip("-")
    return f"{prefix}relatorio-repercussao-midiatica-{slug}.pdf"


@router.get("/projects/{project_id}/export.pdf")
def export_pdf(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=False)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_filename(project.topic, draft=False)}"'
        },
    )


@router.get("/projects/{project_id}/export-draft.pdf")
def export_draft_pdf(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    try:
        content = export_report_pdf(db, project, allow_draft=True)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_filename(project.topic, draft=True)}"'
        },
    )

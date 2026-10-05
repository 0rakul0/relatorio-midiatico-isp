from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.costs import summarize_costs
from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.billing import check_quota
from app.cost_tracker import cost_context
from app.database import get_db
from app.models import GeneratedReport, LLMCall
from app.orchestration import (
    request_cancel,
    resume_run,
    run_snapshot,
    start_qa_refinement,
    start_run,
)
from app.services import run_full_methodology


router = APIRouter(tags=["runs"])


@router.post("/projects/{project_id}/run")
def run_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    check_quota(db, user)
    try:
        with cost_context(project_id=project_id, operation="run_full_methodology"):
            return run_full_methodology(db, project)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/run-async", status_code=202)
def run_project_async(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    check_quota(db, user)
    return {"run": start_run(project_id)}


def _owned_run_or_404(db: Session, user: AuthUser, run_id: str) -> dict:
    snapshot = run_snapshot(run_id)
    if not snapshot:
        raise HTTPException(404, "Execução não encontrada")
    project_or_404(db, user, snapshot.get("project_id"))
    return snapshot


@router.post("/projects/{project_id}/resume-async", status_code=202)
def resume_project_async(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    check_quota(db, user)
    return {"run": resume_run(project_id)}


@router.post("/projects/{project_id}/refine-qa-async", status_code=202)
def refine_project_qa_async(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    check_quota(db, user)
    saved = db.scalar(
        select(GeneratedReport).where(GeneratedReport.project_id == project.id)
    )
    if not saved:
        raise HTTPException(409, "O projeto ainda não possui relatório para refinar")
    return {"run": start_qa_refinement(project_id)}


@router.get("/runs/{run_id}")
def get_run_status(
    run_id: str,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    snapshot = _owned_run_or_404(db, user, run_id)
    calls = db.scalars(
        select(LLMCall).where(LLMCall.run_id == run_id).order_by(LLMCall.id.asc())
    ).all()
    snapshot["costs"] = summarize_costs(calls)
    return snapshot


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    _owned_run_or_404(db, user, run_id)
    accepted = request_cancel(run_id)
    return {
        "accepted": accepted,
        "run": run_snapshot(run_id),
    }

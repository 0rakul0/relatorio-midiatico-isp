from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.cost_tracker import cost_context
from app.database import get_db
from app.models import SearchQuery
from app.services import (
    discover_project_profile,
    plan_queries,
    plan_queries_with_llm,
)


router = APIRouter(tags=["project-search"])


@router.post("/projects/{project_id}/discover-profile")
def discover_profile(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    try:
        with cost_context(project_id=project_id, operation="discover_profile"):
            return discover_project_profile(
                db,
                project_or_404(db, user, project_id),
            )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/plan-searches")
def create_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    return {"created": len(plan_queries(db, project))}


@router.post("/projects/{project_id}/ai/plan-searches")
def create_ai_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    try:
        with cost_context(project_id=project_id, operation="plan_queries"):
            project = project_or_404(db, user, project_id)
            return {"created": len(plan_queries_with_llm(db, project))}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/projects/{project_id}/searches")
def searches(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    rows = db.scalars(
        select(SearchQuery).where(SearchQuery.project_id == project_id)
    ).all()
    return [
        {
            "id": row.id,
            "query": row.query,
            "kind": row.kind,
            "purpose": row.purpose,
            "rationale": row.rationale,
            "priority": row.priority,
            "executed_at": row.executed_at,
        }
        for row in rows
    ]

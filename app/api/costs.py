from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user, require_admin
from app.database import get_db
from app.models import LLMCall


router = APIRouter(tags=["costs"])


def costs_rows(rows) -> list[dict]:
    return [
        {
            "id": row.id,
            "project_id": row.project_id,
            "run_id": row.run_id,
            "operation": row.operation,
            "schema_name": row.schema_name,
            "caller": row.caller,
            "model": row.model,
            "success": row.success,
            "error": row.error,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cached_input_tokens": row.cached_input_tokens,
            "search_calls": row.search_calls,
            "cost_usd": row.cost_usd,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _add_to_bucket(bucket: dict, call) -> None:
    bucket["cost_usd"] = round(bucket["cost_usd"] + (call.cost_usd or 0.0), 6)
    bucket["input_tokens"] += call.input_tokens or 0
    bucket["output_tokens"] += call.output_tokens or 0
    bucket["calls"] += 1


def summarize_costs(calls) -> dict:
    totals = {
        "total_cost_usd": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cached_input_tokens": 0,
        "total_search_calls": 0,
        "calls": len(calls),
        "by_model": {},
        "by_operation": {},
    }
    for call in calls:
        totals["total_cost_usd"] += call.cost_usd or 0.0
        totals["total_input_tokens"] += call.input_tokens or 0
        totals["total_output_tokens"] += call.output_tokens or 0
        totals["total_cached_input_tokens"] += call.cached_input_tokens or 0
        totals["total_search_calls"] += call.search_calls or 0
        model_bucket = totals["by_model"].setdefault(
            call.model,
            {"model": call.model, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0},
        )
        _add_to_bucket(model_bucket, call)
        op_key = call.operation or "desconhecida"
        op_bucket = totals["by_operation"].setdefault(
            op_key, {"operation": op_key, "cost_usd": 0.0, "calls": 0}
        )
        op_bucket["cost_usd"] += call.cost_usd or 0.0
        op_bucket["calls"] += 1
    totals["total_cost_usd"] = round(totals["total_cost_usd"], 6)
    return totals


@router.get("/costs")
def costs(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
    limit: int = 200,
):
    rows = db.execute(
        select(LLMCall).order_by(LLMCall.id.desc()).limit(max(1, min(limit, 1000)))
    ).scalars().all()
    return {"calls": costs_rows(rows)}


@router.get("/costs/summary")
def costs_summary(
    db: Session = Depends(get_db),
    admin: AuthUser = Depends(require_admin),
):
    return summarize_costs(db.query(LLMCall).all())


@router.get("/projects/{project_id}/costs")
def project_costs(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    calls = db.scalars(
        select(LLMCall)
        .where(LLMCall.project_id == project_id)
        .order_by(LLMCall.id.desc())
    ).all()
    return {
        "project_id": project_id,
        "calls": costs_rows(calls),
        "summary": summarize_costs(calls),
    }

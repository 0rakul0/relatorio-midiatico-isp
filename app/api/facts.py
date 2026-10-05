from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.database import get_db
from app.fact_layer import fact_assertions_for_report, fact_events_for_report
from app.models import FactEvent, OfficialFact
from app.schemas import OfficialFactCreate


router = APIRouter(tags=["facts"])


@router.post("/projects/{project_id}/official-facts", status_code=201)
def add_official_fact(
    project_id: int,
    payload: OfficialFactCreate,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    row = OfficialFact(project_id=project_id, **payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.get("/projects/{project_id}/official-facts")
def official_facts(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    rows = db.scalars(
        select(OfficialFact).where(OfficialFact.project_id == project_id)
    ).all()
    return [
        {
            "id": row.id,
            "label": row.label,
            "value": row.value,
            "source_reference": row.source_reference,
            "page": row.page,
            "evidence": row.evidence,
            "indicator": row.indicator,
            "geography": row.geography,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "unit": row.unit,
        }
        for row in rows
    ]


@router.get("/projects/{project_id}/facts")
def facts(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    return fact_events_for_report(db, project_id)


@router.get("/projects/{project_id}/facts/{event_id}/evidence")
def fact_evidence(
    project_id: int,
    event_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    event = db.scalar(
        select(FactEvent).where(
            FactEvent.id == event_id,
            FactEvent.project_id == project_id,
        )
    )
    if not event:
        raise HTTPException(404, "Evento factual não encontrado")
    return [
        item
        for item in fact_assertions_for_report(db, project_id)
        if item["event_id"] == event_id
    ]

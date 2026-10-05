from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.billing import check_quota
from app.database import get_db
from app.models import (
    AcademicPaper,
    Classification,
    FactAssertion,
    FactEvent,
    GeneratedReport,
    MediaItem,
    OfficialFact,
    Project,
    SearchQuery,
)
from app.orchestration import start_qa_refinement
from app.services import cached_report_for_project, cached_report_for_topic


router = APIRouter(tags=["reports"])


@router.get("/reports/history")
def report_history(
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    statement = (
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .order_by(GeneratedReport.generated_at.desc())
        .limit(100)
    )
    if not user.is_admin:
        statement = statement.where(Project.owner_id == user.id)
    rows = db.execute(statement).all()
    history, seen_versions = [], set()
    for project, generated in rows:
        generated_day = (
            generated.generated_at.date().isoformat()
            if generated.generated_at else "sem-data"
        )
        version_key = (project.topic.strip().casefold(), generated_day)
        if version_key in seen_versions:
            continue
        seen_versions.add(version_key)
        history.append({
            "id": project.id,
            "topic": project.topic,
            "institution": project.institution,
            "project_type": project.project_type,
            "generated_at": generated.generated_at,
            "qa_status": generated.qa_status,
        })
        if len(history) == 20:
            break
    return history


@router.get("/reports/cache")
def cached_report(
    topic: str,
    collection_start: date | None = None,
    collection_end: date | None = None,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    report = cached_report_for_topic(
        db,
        topic,
        collection_start,
        collection_end,
        owner_id=None if user.is_admin else user.id,
    )
    return {"cached": bool(report), "report": report}


@router.post("/reports/history/{project_id}/refine", status_code=202)
def refine_historical_report(
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


@router.get("/reports/history/{project_id}")
def historical_report(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    report = cached_report_for_project(db, project_id)
    if not report:
        raise HTTPException(404, "Versão do relatório não encontrada")
    return {"report": report}


@router.delete("/reports/history/{project_id}", status_code=204)
def delete_historical_report(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    target = db.execute(
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(Project.id == project_id)
    ).first()
    if not target:
        raise HTTPException(404, "Versão do relatório não encontrada")

    event_ids = select(FactEvent.id).where(FactEvent.project_id == project_id)
    item_ids = select(MediaItem.id).where(MediaItem.project_id == project_id)
    db.execute(delete(FactAssertion).where(FactAssertion.event_id.in_(event_ids)))
    db.execute(delete(FactEvent).where(FactEvent.project_id == project_id))
    db.execute(delete(Classification).where(Classification.media_item_id.in_(item_ids)))
    db.execute(delete(GeneratedReport).where(GeneratedReport.project_id == project_id))
    db.execute(delete(OfficialFact).where(OfficialFact.project_id == project_id))
    db.execute(delete(AcademicPaper).where(AcademicPaper.project_id == project_id))
    db.execute(delete(MediaItem).where(MediaItem.project_id == project_id))
    db.execute(delete(SearchQuery).where(SearchQuery.project_id == project_id))
    db.execute(delete(Project).where(Project.id == project_id))
    db.commit()
    return Response(status_code=204)

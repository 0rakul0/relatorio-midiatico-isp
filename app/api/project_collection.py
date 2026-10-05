from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.cost_tracker import cost_context
from app.database import get_db
from app.models import MediaItem, SearchQuery
from app.schemas import ManualMediaItemCreate
from app.services import (
    canonicalize,
    classify_with_llm,
    collect_web,
    metrics,
    validate_and_classify,
)
from app.services.collection.media_origin import classify_media_origin


router = APIRouter(tags=["project-collection"])


@router.post("/projects/{project_id}/collect")
def collect(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    try:
        return {"added": collect_web(db, project_id)}
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/projects/{project_id}/media-items", status_code=201)
def add_manual_item(
    project_id: int,
    payload: ManualMediaItemCreate,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    url = str(payload.url)
    canonical = canonicalize(url)
    exists = db.scalar(
        select(MediaItem.id).where(
            MediaItem.project_id == project_id,
            MediaItem.canonical_url == canonical,
        )
    )
    if exists:
        raise HTTPException(409, "URL já existe no corpus")

    if payload.query_id is not None:
        query = db.scalar(
            select(SearchQuery).where(
                SearchQuery.id == payload.query_id,
                SearchQuery.project_id == project_id,
            )
        )
        if not query:
            raise HTTPException(
                422,
                "query_id não pertence a este projeto",
            )

    row = MediaItem(
        project_id=project_id,
        title=payload.title,
        url=url,
        canonical_url=canonical,
        domain=payload.url.host,
        published_at=payload.published_at,
        snippet=payload.snippet,
        content=payload.content,
        source_name=payload.source_name,
        view_count=payload.view_count,
        query_id=payload.query_id,
        search_source="manual",
        media_origin=classify_media_origin(url, payload.url.host),
        discovery_purposes=[payload.purpose],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id}


@router.post("/projects/{project_id}/validate-and-classify")
def validate(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    return validate_and_classify(
        db,
        project_or_404(db, user, project_id),
    )


@router.post("/projects/{project_id}/ai/classify")
def classify_ai(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    try:
        with cost_context(project_id=project_id, operation="classify"):
            return classify_with_llm(
                db,
                project_or_404(db, user, project_id),
            )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/projects/{project_id}/metrics")
def get_metrics(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project_or_404(db, user, project_id)
    return metrics(db, project_id)

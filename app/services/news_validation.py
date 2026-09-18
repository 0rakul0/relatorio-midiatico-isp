"""Stage 4: hydrate deduplicated URLs and validate the media corpus."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MediaItem, Project
from app.services.article_hydration import hydrate_media_items
from app.services.collection.common import inferred_publication_date, media_window
from app.services.validation import validate_and_classify


_FACT_PURPOSES = {"FACT_DISCOVERY", "OFFICIAL_FACT", "NOMINAL_FOLLOWUP"}


def _hydration_candidates(db: Session, project: Project) -> list[MediaItem]:
    """Cheap preselection before network hydration.

    Known out-of-window media-only items do not need their full page downloaded,
    but factual/official sources remain eligible because they may prove a fact
    even when they do not count as media repercussion in the requested window.
    """
    items = db.scalars(
        select(MediaItem).where(MediaItem.project_id == project.id).order_by(MediaItem.id.asc())
    ).all()
    start, end = media_window(project)
    enforce_window = bool(project.has_custom_date_window and start and end)

    selected: list[MediaItem] = []
    for item in items:
        purposes = set(item.discovery_purposes or [])
        factual_source = bool(purposes.intersection(_FACT_PURPOSES))
        publication_date = inferred_publication_date(item)
        if (
            enforce_window
            and publication_date
            and not start <= publication_date <= end
            and not factual_source
        ):
            continue
        selected.append(item)
    return selected


def validate_news_stage(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    hydration = hydrate_media_items(
        db,
        project,
        items=_hydration_candidates(db, project),
        cancel_check=cancel_check,
        progress_detail=progress_detail,
    )
    if cancel_check:
        cancel_check()
    validation = validate_and_classify(
        db,
        project,
        cancel_check=cancel_check,
        progress_detail=progress_detail,
    )
    validation["hydration"] = hydration
    return validation

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.models import Project


def project_or_404(db: Session, user: AuthUser, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Projeto não encontrado")
    if project.owner_id != user.id and not user.is_admin:
        raise HTTPException(404, "Projeto não encontrado")
    return project

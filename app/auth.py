"""Autenticação via Supabase Auth (fail-closed) + identidade local.

Todo request autenticado resolve para AuthUser (JWT validado no servidor).
Usuários são provisionados automaticamente no primeiro login; admin via
ADMIN_EMAILS. Sem Sentry: sem token válido, 401.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import AppUser

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    is_admin: bool
    plan: str


def _supabase_client():
    from supabase import create_client

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_key:
        raise RuntimeError("SUPABASE_URL/SUPABASE_KEY não configuradas")
    return create_client(settings.supabase_url, settings.supabase_key)


def _admin_emails() -> set[str]:
    return {
        item.strip().casefold()
        for item in (get_settings().admin_emails or "").split(",")
        if item.strip()
    }


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> AuthUser:
    settings = get_settings()
    if settings.local_auth_bypass:
        # Perfil explicitamente local: preserva o fluxo de ownership, quotas
        # e permissões sem exigir que a máquina alcance o Supabase Auth.
        row = db.get(AppUser, "local-development")
        if row is None:
            row = AppUser(
                id="local-development",
                email="local@development.invalid",
                plan="INSTITUCIONAL",
                is_admin=True,
            )
            db.add(row)
            db.commit()
        return AuthUser(id=row.id, email=row.email, is_admin=True, plan="INSTITUCIONAL")
    if credentials is None or not credentials.credentials:
        raise HTTPException(401, "Autenticação necessária")
    try:
        response = _supabase_client().auth.get_user(credentials.credentials)
        supa_user = response.user
        uid, email = str(supa_user.id), str(supa_user.email or "")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Token inválido ou expirado")
    if not uid:
        raise HTTPException(401, "Token inválido ou expirado")

    row = db.get(AppUser, uid)
    if row is None:
        row = AppUser(id=uid, email=email, plan="FREE",
                      is_admin=email.casefold() in _admin_emails())
        db.add(row)
        db.commit()
    elif email.casefold() in _admin_emails() and not row.is_admin:
        row.is_admin = True
        db.commit()
    return AuthUser(id=row.id, email=row.email, is_admin=bool(row.is_admin),
                    plan=row.plan or "FREE")


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(403, "Acesso administrativo necessário")
    return user

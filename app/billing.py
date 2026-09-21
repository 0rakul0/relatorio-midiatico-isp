"""Planos, quotas e billing (Mercado Pago; webhook ainda MOCK).

Tiers mapeados para os botões que o código já tem: contagem de relatórios,
gasto LLM e recursos por perfil. Quotas calculadas on-demand a partir de
report_runs + llm_calls (sem tabela de ledger).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import AuthUser, get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import AppUser, LLMCall, Project, ReportRun, Subscription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

PLANS: dict[str, dict] = {
    "FREE": {
        "reports_per_month": 2,
        "llm_usd_per_month": 1.0,
        "profiles": ["MIDIATICO_SIMPLES"],
        "youtube": True,
        "fact_layer": False,
        "nominal_followup": False,
        "gap_fill": False,
        "llm_qa": True,
    },
    "PRO": {
        "reports_per_month": 20,
        "llm_usd_per_month": 10.0,
        "profiles": ["AUTO", "MIDIATICO_SIMPLES", "MIDIATICO_COM_FATOS", "COMPLETO_NOMINAL"],
        "youtube": True,
        "fact_layer": True,
        "nominal_followup": True,
        "gap_fill": True,
        "llm_qa": True,
    },
    "INSTITUCIONAL": {
        "reports_per_month": 10**9,
        "llm_usd_per_month": 10**9,
        "profiles": ["AUTO", "MIDIATICO_SIMPLES", "MIDIATICO_COM_FATOS", "COMPLETO_NOMINAL"],
        "youtube": True,
        "fact_layer": True,
        "nominal_followup": True,
        "gap_fill": True,
        "llm_qa": True,
    },
}


def plan_for(user: AuthUser) -> dict:
    return PLANS.get((user.plan or "FREE").upper(), PLANS["FREE"])


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def margin_multiplier() -> float:
    return 1.0 + max(0.0, float(get_settings().billing_margin_pct or 0.0)) / 100.0


def monthly_usage(db: Session, user_id: str) -> dict:
    start = _month_start()
    reports = db.scalar(
        select(func.count(func.distinct(ReportRun.run_id)))
        .join(Project, Project.id == ReportRun.project_id)
        .where(Project.owner_id == user_id, ReportRun.created_at >= start)
    ) or 0
    raw = db.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
        .join(Project, Project.id == LLMCall.project_id)
        .where(Project.owner_id == user_id, LLMCall.created_at >= start)
    ) or 0.0
    return {
        "reports_started": int(reports),
        "llm_usd_raw": round(float(raw), 6),
        "llm_usd": round(float(raw) * margin_multiplier(), 6),
        "margin_pct": float(get_settings().billing_margin_pct or 0.0),
    }


def check_quota(db: Session, user: AuthUser) -> dict:
    """402 quando o plano estourou (relatórios ou gasto LLM no mês)."""
    plan = plan_for(user)
    usage = monthly_usage(db, user.id)
    if usage["reports_started"] >= plan["reports_per_month"]:
        raise HTTPException(
            402,
            f"Limite do plano {user.plan}: {plan['reports_per_month']} relatório(s)/mês "
            f"({usage['reports_started']} usados).",
        )
    if usage["llm_usd"] >= plan["llm_usd_per_month"]:
        raise HTTPException(
            402,
            f"Limite do plano {user.plan}: US$ {plan['llm_usd_per_month']}/mês em LLM "
            f"(US$ {usage['llm_usd']} usados).",
        )
    return usage


def clamp_profile(plan: dict, requested: str) -> tuple[str, str | None]:
    """Força perfil permitido; devolve (perfil, aviso|None)."""
    allowed = plan.get("profiles") or ["MIDIATICO_SIMPLES"]
    if (requested or "AUTO") in allowed:
        return requested, None
    fallback = "MIDIATICO_SIMPLES"
    return fallback, f"Perfil ajustado para {fallback} (plano atual)."


def plan_allows(db: Session, project: Project, feature: str) -> bool:
    owner_id = getattr(project, "owner_id", None)
    if not owner_id:
        return True  # legado sem dono: comportamento antigo
    owner = db.get(AppUser, owner_id)
    plan = PLANS.get(((owner.plan if owner else None) or "FREE").upper(), PLANS["FREE"])
    return bool(plan.get(feature, False))


class CheckoutIn(BaseModel):
    plan: str


class WebhookIn(BaseModel):
    user_email: str | None = None
    user_id: str | None = None
    plan: str = "PRO"
    status: str = "active"
    mp_preapproval_id: str | None = None


@router.get("/plans")
def list_plans(user: AuthUser = Depends(get_current_user)):
    return {"plans": PLANS, "mine": user.plan}


@router.get("/me")
def billing_me(db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    return {"plan": user.plan, "usage": monthly_usage(db, user.id), "limits": plan_for(user)}


@router.post("/checkout", status_code=501)
def checkout(
    payload: CheckoutIn,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    # MOCK: aqui nasceria a preferência Mercado Pago (checkout URL real).
    if payload.plan.upper() not in PLANS:
        raise HTTPException(422, "Plano desconhecido")
    sub = db.get(Subscription, user.id)
    if sub is None:
        sub = Subscription(user_id=user.id, plan=payload.plan.upper(), status="pending")
        db.add(sub)
    else:
        sub.plan = payload.plan.upper()
        sub.status = "pending"
    db.commit()
    logger.warning("billing MOCK: checkout %s para %s (MP não integrado)", payload.plan, user.id)
    return {
        "mock": True,
        "plan": payload.plan.upper(),
        "status": "pending",
        "message": "Integração Mercado Pago pendente: finalize em POST /billing/webhook (mock).",
    }


@router.post("/webhook")
def webhook(payload: WebhookIn, db: Session = Depends(get_db)):
    # MOCK: o webhook real valida assinatura MP e resolve o usuário pelo
    # preapproval_id. Aqui aceitamos user_email/user_id direto (só dev).
    logger.warning("billing MOCK: webhook %s -> %s", payload.plan, payload.user_email or payload.user_id)
    target = None
    if payload.user_id:
        target = db.get(AppUser, payload.user_id)
    elif payload.user_email:
        target = db.scalar(select(AppUser).where(AppUser.email == payload.user_email))
    if target is None:
        raise HTTPException(404, "Usuário não encontrado")
    if payload.plan.upper() not in PLANS:
        raise HTTPException(422, "Plano desconhecido")
    target.plan = payload.plan.upper()
    sub = db.get(Subscription, target.id)
    if sub is None:
        sub = Subscription(user_id=target.id)
        db.add(sub)
    sub.plan = payload.plan.upper()
    sub.status = payload.status
    sub.mp_preapproval_id = payload.mp_preapproval_id
    db.commit()
    return {"ok": True, "user_id": target.id, "plan": target.plan, "status": sub.status}


@router.get("/subscriptions")
def list_subscriptions(
    db: Session = Depends(get_db), admin: AuthUser = Depends(require_admin)
):
    rows = db.scalars(select(Subscription)).all()
    return [
        {"user_id": row.user_id, "plan": row.plan, "status": row.status,
         "updated_at": row.updated_at}
        for row in rows
    ]

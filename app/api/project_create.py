from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import AuthUser, get_current_user
from app.billing import clamp_profile, plan_for
from app.database import get_db
from app.models import Project
from app.schemas import ProjectCreate
from app.topic_profile import requested_topic_window


router = APIRouter(tags=["projects"])


BRAZILIAN_STATES = frozenset({
    "Acre", "Alagoas", "Amapá", "Amazonas", "Bahia", "Ceará", "Distrito Federal",
    "Espírito Santo", "Goiás", "Maranhão", "Mato Grosso", "Mato Grosso do Sul",
    "Minas Gerais", "Pará", "Paraíba", "Paraná", "Pernambuco", "Piauí",
    "Rio de Janeiro", "Rio Grande do Norte", "Rio Grande do Sul", "Rondônia",
    "Roraima", "Santa Catarina", "São Paulo", "Sergipe", "Tocantins",
})


@router.post("/projects", status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), user: AuthUser = Depends(get_current_user)):
    raw_scopes = payload.geographic_scopes or ([payload.geographic_scope] if payload.geographic_scope else [])
    geographic_scopes = list(dict.fromkeys(
        " ".join(str(scope or "").split()) for scope in raw_scopes
        if " ".join(str(scope or "").split())
    ))
    invalid_scopes = [scope for scope in geographic_scopes if scope not in BRAZILIAN_STATES]
    if invalid_scopes:
        raise HTTPException(422, "Estado inválido para o recorte geográfico")
    geographic_suffix = (
        "" if not geographic_scopes
        else f" no estado de {geographic_scopes[0]}" if len(geographic_scopes) == 1
        else f" nos estados de {', '.join(geographic_scopes)}"
    )
    scoped_topic = f"{payload.topic}{geographic_suffix}"
    if len(scoped_topic) > 300:
        raise HTTPException(422, "Tema e recorte geográfico excedem 300 caracteres")
    today = date.today()
    inferred_window = requested_topic_window(scoped_topic)

    explicit_collection = bool(payload.collection_start or payload.collection_end)
    explicit_event = bool(payload.event_start or payload.event_end)

    # Regra temporal:
    # 1) janela de repercussão explicitamente informada sempre vence;
    # 2) se o usuário informou apenas a janela factual, a repercussão herda
    #    exatamente essa janela (comportamento mais seguro e previsível);
    # 3) caso contrário, um ano/mês explícito no tema define ambas as janelas;
    # 4) sem qualquer recorte, a pesquisa permanece temática.
    if explicit_collection:
        collection_start = payload.collection_start or payload.collection_end
        collection_end = payload.collection_end or payload.collection_start
        collection_window_source = "USER"
        has_custom_window = True
    elif explicit_event:
        collection_start = payload.event_start or payload.event_end
        collection_end = payload.event_end or payload.event_start
        collection_window_source = "EVENT_WINDOW"
        has_custom_window = True
    elif inferred_window:
        collection_start, collection_end = inferred_window
        collection_window_source = "TOPIC"
        has_custom_window = True
    else:
        # Compatibilidade com o schema legado: collection_start/end ainda são
        # NOT NULL no banco, então guardamos hoje apenas como placeholder técnico.
        # has_custom_date_window=False é a fonte de verdade: query_window()
        # devolve (None, None), portanto nenhuma busca recebe filtro de um dia.
        # project_payload() também oculta esse placeholder do relatório final.
        collection_start = collection_end = today
        collection_window_source = "TOPIC_DRIVEN"
        has_custom_window = False

    if explicit_event:
        event_start = payload.event_start or payload.event_end
        event_end = payload.event_end or payload.event_start
        event_window_source = "USER"
    elif inferred_window:
        event_start, event_end = inferred_window
        event_window_source = "TOPIC"
    elif has_custom_window:
        event_start, event_end = collection_start, collection_end
        event_window_source = "COLLECTION_WINDOW"
    else:
        # Sem datas explícitas, não inventamos uma janela factual de um único dia.
        event_start = event_end = None
        event_window_source = "TOPIC_DRIVEN"

    if collection_end < collection_start:
        raise HTTPException(422, "collection_end deve ser posterior ao início")
    if event_start and event_end and event_end < event_start:
        raise HTTPException(422, "event_end deve ser posterior ao início")

    execution_options = {
        key: value
        for key, value in {
            "enable_fact_layer": payload.enable_fact_layer,
            "enable_nominal_followup": payload.enable_nominal_followup,
            "enable_academic_research": payload.enable_academic_research,
        }.items()
        if value is not None
    }
    execution_options["collection_window_source"] = collection_window_source
    execution_options["event_window_source"] = event_window_source
    execution_options["temporal_mode"] = (
        "EXPLICIT_WINDOW" if has_custom_window else "TOPIC_DRIVEN"
    )
    execution_options["geographic_scopes"] = geographic_scopes
    execution_options["geographic_scope"] = geographic_scopes[0] if len(geographic_scopes) == 1 else "NACIONAL"
    # O campo launch_date do banco continua preenchido por compatibilidade com
    # instalações antigas, mas só deve ser exibido como dado editorial quando
    # o usuário o informou ou quando o agente documentalista o confirmou.
    if payload.launch_date is not None:
        execution_options["launch_date_user_supplied"] = True

    # Limites do plano: perfil permitido + recursos vetados.
    plan = plan_for(user)
    execution_profile, plan_notice = clamp_profile(plan, payload.execution_profile)
    if not plan["fact_layer"]:
        execution_options["enable_fact_layer"] = False
    if not plan["nominal_followup"]:
        execution_options["enable_nominal_followup"] = False

    row = Project(
        topic=scoped_topic,
        institution=payload.institution,
        owner_id=user.id,
        launch_date=payload.launch_date or today,
        collection_start=collection_start,
        collection_end=collection_end,
        event_start=event_start,
        event_end=event_end,
        execution_profile=execution_profile,
        execution_options=execution_options,
        fact_grace_days=10,
        has_custom_date_window=has_custom_window,
        project_type="AUTO",
        status="CUSTOM_DATES" if has_custom_window else "DRAFT",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "id": row.id,
        "status": row.status,
        "plan": user.plan,
        "plan_notice": plan_notice,
        "discovery": {
            "status": "DEFERRED",
            "message": "O perfil será executado como a primeira etapa acompanhada do relatório.",
        },
    }

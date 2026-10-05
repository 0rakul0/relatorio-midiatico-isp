from __future__ import annotations

from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.billing import clamp_profile, plan_for
from app.database import get_db
from app.fact_layer import fact_events_for_report
from app.models import (
    AcademicPaper,
    Classification,
    Project,
)
from app.schemas import ProjectCreate
from app.services import (
)
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


@router.get("/projects/{project_id}/research-plan")
def project_research_plan(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Expõe a interpretação do tema e a estratégia de busca sem nova chamada à LLM."""
    project = project_or_404(db, user, project_id)
    profile = project.topic_profile or {}
    options = project.execution_options or {}
    execution_plan = project.execution_plan or {}

    queries = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project_id)
        .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
    ).all()

    project_type_labels = {
        "INSTITUTIONAL_PRODUCT": "Produto institucional",
        "EVENT_TOPIC": "Tema factual / evento",
        "GENERAL_TOPIC": "Tema geral",
        "AUTO": "Classificação automática",
    }

    primary_anchor = (
        profile.get("product_name")
        or profile.get("event_anchor")
        or profile.get("product_anchor")
        or project.topic
    )

    variants = []
    for key in (
        "product_search_variants",
        "event_search_variants",
        "fact_discovery_variants",
        "search_synonyms",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            variants.extend(str(item).strip() for item in value if str(item).strip())
    variants = list(dict.fromkeys(variants))

    entities = {
        "atores": list(profile.get("actors") or []),
        "ações": list(profile.get("actions") or []),
        "locais": list(profile.get("locations") or []),
        "organizações": list(profile.get("organizations") or []),
        "assuntos": list(profile.get("subject_terms") or []),
    }

    return {
        "project_id": project.id,
        "original_topic": project.topic,
        "interpreted_topic": primary_anchor,
        "project_type": project.project_type,
        "project_type_label": project_type_labels.get(project.project_type, project.project_type),
        "temporal_mode": options.get("temporal_mode"),
        "collection_window": {
            "start": project.collection_start.isoformat() if project.has_custom_date_window and project.collection_start else None,
            "end": project.collection_end.isoformat() if project.has_custom_date_window and project.collection_end else None,
            "source": options.get("collection_window_source"),
        },
        "event_window": {
            "start": project.event_start.isoformat() if project.event_start else None,
            "end": project.event_end.isoformat() if project.event_end else None,
            "source": options.get("event_window_source"),
        },
        "geographic_scopes": list(options.get("geographic_scopes") or []),
        "corrections_and_variants": variants,
        "entities": entities,
        "inclusion_rules": list(profile.get("inclusion_rules") or []),
        "exclusion_rules": list(profile.get("exclusion_rules") or []),
        "search_strategy": profile.get("search_strategy") or {},
        "execution_plan": execution_plan,
        "queries": [
            {
                "id": row.id,
                "query": row.query,
                "kind": row.kind,
                "purpose": row.purpose,
                "rationale": row.rationale,
                "priority": row.priority,
                "execution_status": row.execution_status,
                "executed_at": row.executed_at.isoformat() if row.executed_at else None,
                "providers_attempted": list(row.providers_attempted or []),
                "results_returned": row.results_returned,
                "results_accepted": row.results_accepted,
            }
            for row in queries
        ],
    }



@router.get("/projects/{project_id}/execution-preview")
def project_execution_preview(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """Prévia auditável das etapas enquanto o relatório ainda está sendo executado."""
    project = project_or_404(db, user, project_id)

    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id == project_id)
        .order_by(MediaItem.id.desc())
    ).all()
    queries = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project_id)
        .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
    ).all()

    def item_origin(item: MediaItem) -> str:
        return item.media_origin or classify_media_origin(item.url, item.domain)

    domains = Counter(
        (item.domain or "").lower().removeprefix("www.")
        for item in items
        if item.domain
    )
    origins = Counter(item_origin(item) for item in items)
    statuses = Counter(str(item.status or "PENDING") for item in items)
    query_statuses = Counter(str(row.execution_status or "PENDING") for row in queries)

    recent_items = [
        {
            "id": item.id,
            "title": item.title,
            "url": item.url,
            "domain": item.domain,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "status": item.status,
            "media_origin": item_origin(item),
            "search_source": item.search_source,
            "corpus_origin": item.corpus_origin,
        }
        for item in items[:24]
    ]

    classifications = db.scalars(
        select(Classification)
        .join(MediaItem, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id)
    ).all()
    themes = Counter(
        str(row.theme)
        for row in classifications
        if row.theme
    )

    fact_rows = fact_events_for_report(db, project_id)
    fact_statuses = Counter(
        str(row.get("resolution_status") or "PENDING")
        for row in fact_rows
    )

    papers = db.scalars(
        select(AcademicPaper)
        .where(AcademicPaper.project_id == project_id)
        .order_by(AcademicPaper.id.desc())
    ).all()

    return {
        "project_id": project.id,
        "collection": {
            "items_found": len(items),
            "new_items": sum(str(item.corpus_origin or "SEARCH").upper() != "REUSED" for item in items),
            "reused_items": sum(str(item.corpus_origin or "").upper() == "REUSED" for item in items),
            "origins": dict(origins),
            "domains": [
                {"domain": domain, "items": count}
                for domain, count in domains.most_common(20)
            ],
            "recent_items": recent_items,
            "queries": {
                "total": len(queries),
                "executed": sum(row.executed_at is not None for row in queries),
                "statuses": dict(query_statuses),
                "returned": sum(int(row.results_returned or 0) for row in queries),
                "accepted": sum(int(row.results_accepted or 0) for row in queries),
            },
        },
        "social": {
            "social_items": int(origins.get("REDE_SOCIAL", 0)),
            "youtube_items": int(origins.get("YOUTUBE", 0)),
        },
        "validation": {
            "status_counts": dict(statuses),
            "valid": int(statuses.get("VALID", 0)),
            "pending": int(statuses.get("PENDING", 0)),
            "discarded": sum(
                count
                for status, count in statuses.items()
                if status not in {"VALID", "PENDING"}
            ),
        },
        "facts": {
            "events": len(fact_rows),
            "statuses": dict(fact_statuses),
        },
        "classification": {
            "classified_items": len(classifications),
            "themes": [
                {"theme": theme, "items": count}
                for theme, count in themes.most_common(12)
            ],
        },
        "academic": {
            "papers": len(papers),
            "recent": [
                {
                    "title": paper.title_ptbr or paper.title,
                    "provider": paper.provider,
                    "url": paper.url,
                }
                for paper in papers[:8]
            ],
        },
    }

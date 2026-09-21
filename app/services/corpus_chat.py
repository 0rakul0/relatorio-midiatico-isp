"""Conversa com a base: chat grounded exclusivamente no corpus coletado.

O chat NAO tem ferramentas nem faz busca externa. Ele responde apenas com os
itens validados do projeto (``MediaItem.status == "VALID"``), permitindo
auditoria: a resposta chega junto com as fontes (por indice) que a sustentam.

O custo LLM da conversa e associado ao projeto via ``cost_context``.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.cost_tracker import cost_context
from app.llm import llm_is_configured
from app.models import GeneratedReport, MediaItem, Project
from app.schemas import ChatResponse

_CHAT_CORPUS_LIMIT = 60
_CHAT_ITEM_MAX_CHARS = 3000


def list_chat_projects(db: Session, user) -> dict:
    """Projetos do usuario (ou todos, para admin) com resumo do corpus.

    Ordena por ultima geracao de relatorio decrescente (nulls por ultimo) e,
    como desempate, por criacao. ``last_active_project_id`` e o projeto mais
    representativo: o mais recente com relatorio gerado; na falta, o mais
    recente com itens validados; na falta, o projeto mais novo.
    """
    query = select(Project)
    if not user.is_admin:
        query = query.where(Project.owner_id == user.id)
    projects = list(db.scalars(query.order_by(Project.created_at.desc())).all())

    project_ids = [project.id for project in projects]
    if not project_ids:
        return {"projects": [], "last_active_project_id": None}

    counts = dict(
        db.execute(
            select(MediaItem.project_id, func.count(MediaItem.id))
            .where(MediaItem.status == "VALID", MediaItem.project_id.in_(project_ids))
            .group_by(MediaItem.project_id)
        ).all()
    )
    generated_at = dict(
        db.execute(
            select(GeneratedReport.project_id, GeneratedReport.generated_at)
            .where(GeneratedReport.project_id.in_(project_ids))
        ).all()
    )

    rows = [
        {
            "id": project.id,
            "topic": project.topic,
            "project_type": project.project_type,
            "collection_start": project.collection_start.isoformat() if project.collection_start else None,
            "collection_end": project.collection_end.isoformat() if project.collection_end else None,
            "valid_items": int(counts.get(project.id, 0)),
            "generated_at": generated_at.get(project.id).isoformat() if generated_at.get(project.id) else None,
            "created_at": project.created_at.isoformat() if project.created_at else None,
        }
        for project in projects
    ]
    rows.sort(
        key=lambda row: (
            -1 if row["generated_at"] else (-2 if row["valid_items"] else -3),
            row["generated_at"] or row["created_at"] or "",
        ),
        reverse=True,
    )

    last_active = rows[0] if rows else None

    return {
        "projects": rows,
        "last_active_project_id": last_active["id"] if last_active else None,
    }


def _chat_corpus_members(db: Session, project: Project, limit: int = _CHAT_CORPUS_LIMIT) -> list[dict]:
    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id == project.id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.desc().nullslast())
        .limit(limit)
    ).all()

    members: list[dict] = []
    for index, item in enumerate(items):
        text = (item.content or "").strip() or (item.snippet or "").strip()
        members.append(
            {
                "index": index,
                "id": item.id,
                "title": item.title,
                "domain": item.domain,
                "url": item.url,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "source_name": item.source_name,
                "media_origin": item.media_origin,
                "search_source": item.search_source,
                "content": text[:_CHAT_ITEM_MAX_CHARS],
            }
        )
    return members


def chat_with_corpus(db: Session, project: Project, messages: list[dict]) -> dict:
    """Responde uma pergunta do chat usando somente o corpus validado do projeto."""
    if not llm_is_configured():
        raise RuntimeError(
            "Nenhuma LLM configurada. Defina OPENAI_API_KEY (ou use Ollama com "
            "OPENAI_BASE_URL/OPENAI_MODEL) para usar o chat."
        )

    members = _chat_corpus_members(db, project)
    payload = {
        "project": {
            "id": project.id,
            "topic": project.topic,
            "project_type": project.project_type,
            "collection_start": _iso(project.collection_start),
            "collection_end": _iso(project.collection_end),
        },
        "corpus": members,
        "conversation": messages,
    }

    with cost_context(project_id=project.id, operation="corpus_chat", schema_name="chat"):
        result = get_report_agent().run(
            task="chat",
            payload=payload,
            response_model=ChatResponse,
            tools=None,
            max_output_tokens=4000,
        )

    used = [int(value) for value in (result.get("used_member_indices") or []) if value is not None]
    by_index = {member["index"]: member for member in members}
    sources = []
    for index in dict.fromkeys(used):
        member = by_index.get(index)
        if member is None:
            continue
        sources.append(
            {
                "title": member.get("title") or "Fonte",
                "url": member.get("url"),
                "domain": member.get("domain"),
                "published_at": member.get("published_at"),
                "source_name": member.get("source_name"),
                "media_origin": member.get("media_origin"),
                "search_source": member.get("search_source"),
            }
        )

    return {
        "answer": result.get("answer") or "",
        "sources": sources,
        "corpus_size": len(members),
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
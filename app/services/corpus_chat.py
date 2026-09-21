"""Conversa com a base: chat grounded exclusivamente no corpus coletado.

O chat NAO tem ferramentas nem faz busca externa. Projetos com o mesmo tema sao
tratados como uma unica base consolidada: todos os itens VALID desses projetos
sao reunidos e deduplicados por URL canonica antes de montar o contexto.

Isso evita que execucoes repetidas de um mesmo tema aparecam como bases
independentes no seletor e permite que novas coletas complementem o corpus
historico daquele tema.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.cost_tracker import cost_context
from app.llm import llm_is_configured
from app.models import GeneratedReport, MediaItem, Project
from app.schemas import ChatResponse

_CHAT_CORPUS_LIMIT = 60
_CHAT_ITEM_MAX_CHARS = 3000


def _topic_key(value: str | None) -> str:
    """Normaliza o tema apenas para agrupamento de execucoes equivalentes."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _visible_projects(db: Session, user) -> list[Project]:
    query = select(Project)
    if not user.is_admin:
        query = query.where(Project.owner_id == user.id)
    return list(db.scalars(query.order_by(Project.created_at.desc())).all())


def _group_project_ids(db: Session, project: Project) -> list[int]:
    """Retorna todas as execucoes do mesmo proprietario e do mesmo tema."""
    query = select(Project.id, Project.topic).where(Project.owner_id == project.owner_id)
    rows = db.execute(query).all()
    key = _topic_key(project.topic)
    return [project_id for project_id, topic in rows if _topic_key(topic) == key]


def list_chat_projects(db: Session, user) -> dict:
    """Lista uma unica entrada por tema, consolidando todas as execucoes.

    valid_items representa noticias VALID unicas por URL canonica em todas
    as execucoes daquele tema. project_count informa quantas execucoes foram
    incorporadas na base consolidada.
    """
    projects = _visible_projects(db, user)
    if not projects:
        return {"projects": [], "last_active_project_id": None}

    project_ids = [project.id for project in projects]

    generated_at = {
        project_id: generated
        for project_id, generated in db.execute(
            select(GeneratedReport.project_id, GeneratedReport.generated_at)
            .where(GeneratedReport.project_id.in_(project_ids))
        ).all()
    }

    media_rows = db.execute(
        select(MediaItem.id, MediaItem.project_id, MediaItem.canonical_url, MediaItem.url)
        .where(MediaItem.status == "VALID", MediaItem.project_id.in_(project_ids))
    ).all()

    media_by_project: dict[int, set[str]] = {}
    for item_id, project_id, canonical_url, url in media_rows:
        key = (canonical_url or url or f"id:{item_id}").strip()
        media_by_project.setdefault(project_id, set()).add(key)

    groups: dict[str, list[Project]] = {}
    for project in projects:
        groups.setdefault(_topic_key(project.topic), []).append(project)

    rows: list[dict] = []
    for group_projects in groups.values():
        representative = max(
            group_projects,
            key=lambda item: (
                generated_at.get(item.id) is not None,
                generated_at.get(item.id) or item.created_at,
            ),
        )

        urls: set[str] = set()
        for item in group_projects:
            urls.update(media_by_project.get(item.id, set()))

        report_dates = [generated_at[item.id] for item in group_projects if generated_at.get(item.id)]
        latest_generated = max(report_dates) if report_dates else None
        created_dates = [item.created_at for item in group_projects if item.created_at]
        latest_created = max(created_dates) if created_dates else None

        collection_starts = [item.collection_start for item in group_projects if item.collection_start]
        collection_ends = [item.collection_end for item in group_projects if item.collection_end]

        rows.append(
            {
                "id": representative.id,
                "topic": " ".join((representative.topic or "").split()),
                "project_type": representative.project_type,
                "collection_start": min(collection_starts).isoformat() if collection_starts else None,
                "collection_end": max(collection_ends).isoformat() if collection_ends else None,
                "valid_items": len(urls),
                "project_count": len(group_projects),
                "project_ids": [item.id for item in group_projects],
                "generated_at": latest_generated.isoformat() if latest_generated else None,
                "created_at": latest_created.isoformat() if latest_created else None,
            }
        )

    rows.sort(
        key=lambda row: (
            bool(row["generated_at"]),
            row["generated_at"] or row["created_at"] or "",
            row["valid_items"],
        ),
        reverse=True,
    )

    available = [row for row in rows if row["valid_items"] > 0]
    last_active = available[0] if available else (rows[0] if rows else None)

    return {
        "projects": rows,
        "last_active_project_id": last_active["id"] if last_active else None,
    }


def _chat_corpus_members(
    db: Session,
    project: Project,
    limit: int = _CHAT_CORPUS_LIMIT,
) -> tuple[list[dict], int, list[int]]:
    project_ids = _group_project_ids(db, project) or [project.id]
    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id.in_(project_ids), MediaItem.status == "VALID")
    ).all()

    unique: dict[str, MediaItem] = {}
    for item in items:
        key = (item.canonical_url or item.url or f"id:{item.id}").strip()
        current = unique.get(key)
        if current is None:
            unique[key] = item
            continue
        current_text = (current.content or current.snippet or "").strip()
        candidate_text = (item.content or item.snippet or "").strip()
        if len(candidate_text) > len(current_text):
            unique[key] = item

    ordered = sorted(
        unique.values(),
        key=lambda item: (item.published_at or date.min, item.id or 0),
        reverse=True,
    )
    total_unique = len(ordered)
    selected = ordered[:limit]

    members: list[dict] = []
    for index, item in enumerate(selected):
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
    return members, total_unique, project_ids


def chat_with_corpus(db: Session, project: Project, messages: list[dict]) -> dict:
    """Responde usando a base consolidada de todas as execucoes do mesmo tema."""
    if not llm_is_configured():
        raise RuntimeError(
            "Nenhuma LLM configurada. Defina OPENAI_API_KEY (ou use Ollama com "
            "OPENAI_BASE_URL/OPENAI_MODEL) para usar o chat."
        )

    members, total_unique, project_ids = _chat_corpus_members(db, project)
    payload = {
        "project": {
            "id": project.id,
            "project_ids": project_ids,
            "topic": project.topic,
            "project_type": project.project_type,
            "collection_start": _iso(project.collection_start),
            "collection_end": _iso(project.collection_end),
            "consolidated_runs": len(project_ids),
            "unique_valid_items": total_unique,
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
        "corpus_size": total_unique,
        "context_size": len(members),
        "project_count": len(project_ids),
    }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None

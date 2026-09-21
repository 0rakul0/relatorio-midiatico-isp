"""Persistencia do historico do chat com o corpus.

Conversas pertencem ao usuario e a um escopo logico:
- TOPIC: todas as execucoes consolidadas do mesmo tema;
- ALL: todo o acervo visivel ao usuario.

O historico guarda texto, fontes exibidas e metadados de retrieval para que a
interface possa reconstruir exatamente a conversa sem chamar a LLM novamente.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ChatConversation, ChatMessage, Project
from app.services.corpus_chat import _topic_key


ALL_SCOPE_KEY = "__all__"


def _scope(project: Project | None) -> tuple[str, str, str]:
    if project is None:
        return "ALL", ALL_SCOPE_KEY, "Todo o acervo"
    return "TOPIC", _topic_key(project.topic), " ".join((project.topic or "").split())


def _serialize_conversation(db: Session, row: ChatConversation) -> dict:
    message_count = db.scalar(
        select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == row.id)
    ) or 0
    return {
        "id": row.id,
        "scope_type": row.scope_type,
        "scope_key": row.scope_key,
        "topic": row.topic,
        "title": row.title,
        "message_count": int(message_count),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_chat_conversations(
    db: Session,
    *,
    owner_id: str,
    project: Project | None,
    limit: int = 20,
) -> list[dict]:
    scope_type, scope_key, _topic = _scope(project)
    rows = db.scalars(
        select(ChatConversation)
        .where(
            ChatConversation.owner_id == owner_id,
            ChatConversation.scope_type == scope_type,
            ChatConversation.scope_key == scope_key,
        )
        .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
        .limit(max(1, min(int(limit or 20), 50)))
    ).all()
    return [_serialize_conversation(db, row) for row in rows]


def get_chat_conversation(
    db: Session,
    *,
    owner_id: str,
    conversation_id: int,
) -> dict | None:
    row = db.get(ChatConversation, conversation_id)
    if row is None or row.owner_id != owner_id:
        return None

    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == row.id)
        .order_by(ChatMessage.id.asc())
    ).all()
    payload = _serialize_conversation(db, row)
    payload["messages"] = [
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "sources": list(message.sources or []),
            "metadata": dict(message.extra or {}),
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
        for message in messages
    ]
    return payload


def persist_chat_exchange(
    db: Session,
    *,
    owner_id: str,
    project: Project | None,
    conversation_id: int | None,
    question: str,
    answer_state: dict,
    history_messages: list[dict] | None = None,
) -> ChatConversation:
    scope_type, scope_key, topic = _scope(project)
    conversation = db.get(ChatConversation, conversation_id) if conversation_id else None

    if conversation_id and conversation is None:
        raise ValueError("Conversa não encontrada")

    is_new = conversation is None
    if conversation is not None:
        if conversation.owner_id != owner_id:
            raise PermissionError("Conversa não pertence ao usuário")
        if conversation.scope_type != scope_type or conversation.scope_key != scope_key:
            raise ValueError("Conversa pertence a outro tema")
    else:
        clean_title = " ".join((question or "").split()).strip()
        conversation = ChatConversation(
            owner_id=owner_id,
            scope_type=scope_type,
            scope_key=scope_key,
            topic=topic,
            title=(clean_title[:120] or "Nova conversa"),
        )
        db.add(conversation)
        db.flush()

    if is_new and history_messages:
        # Compatibilidade com sessões que ainda estavam abertas antes da
        # persistência: aproveita o contexto já presente no navegador.
        for message in history_messages:
            role = str(message.get("role") or "").lower()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            db.add(
                ChatMessage(
                    conversation_id=conversation.id,
                    role=role,
                    content=content,
                    sources=[],
                    extra={"backfilled": True},
                )
            )
    else:
        db.add(
            ChatMessage(
                conversation_id=conversation.id,
                role="user",
                content=question,
                sources=[],
                extra={},
            )
        )

    db.add(
        ChatMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=str(answer_state.get("answer") or ""),
            sources=list(answer_state.get("sources") or []),
            extra={
                "corpus_size": int(answer_state.get("corpus_size") or 0),
                "context_size": int(answer_state.get("context_size") or 0),
                "project_count": int(answer_state.get("project_count") or 0),
                "retrieval_strategy": answer_state.get("retrieval_strategy"),
            },
        )
    )
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conversation)
    return conversation


def delete_chat_conversation(
    db: Session,
    *,
    owner_id: str,
    conversation_id: int,
) -> bool:
    row = db.get(ChatConversation, conversation_id)
    if row is None or row.owner_id != owner_id:
        return False
    db.delete(row)
    db.commit()
    return True

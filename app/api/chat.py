from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.database import get_db
from app.models import Project
from app.schemas import ChatAskRequest
from app.services import (
    chat_with_all_corpus,
    chat_with_corpus,
    delete_chat_conversation,
    get_chat_conversation,
    list_chat_conversations,
    list_chat_projects,
    persist_chat_exchange,
)


router = APIRouter(tags=["chat"])


def _chat_project_from_scope(project_id: str, db: Session, user: AuthUser) -> Project | None:
    if str(project_id).casefold() == "all":
        return None
    try:
        numeric_id = int(project_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "project_id inválido") from exc
    return project_or_404(db, user, numeric_id)


def _last_chat_question(payload: ChatAskRequest) -> str:
    for message in reversed(payload.messages):
        if message.role == "user":
            return message.content
    raise HTTPException(422, "A conversa precisa terminar com uma pergunta do usuário")


@router.get("/chat", include_in_schema=False)
def chat_page():
    return FileResponse("app/static/chat.html")


@router.get("/chat/projects")
def chat_projects(
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    return list_chat_projects(db, user)


@router.get("/chat/conversations")
def chat_conversations(
    project_id: str,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = _chat_project_from_scope(project_id, db, user)
    return {
        "conversations": list_chat_conversations(
            db,
            owner_id=user.id,
            project=project,
        )
    }


@router.get("/chat/conversations/{conversation_id}")
def chat_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    conversation = get_chat_conversation(
        db,
        owner_id=user.id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(404, "Conversa não encontrada")
    return conversation


@router.delete("/chat/conversations/{conversation_id}", status_code=204)
def delete_chat_conversation_route(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    if not delete_chat_conversation(
        db,
        owner_id=user.id,
        conversation_id=conversation_id,
    ):
        raise HTTPException(404, "Conversa não encontrada")
    return Response(status_code=204)


@router.post("/chat/all/ask")
def chat_all_ask(
    payload: ChatAskRequest,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    messages = [message.model_dump(mode="json") for message in payload.messages]
    try:
        state = chat_with_all_corpus(db, user, messages)
        conversation = persist_chat_exchange(
            db,
            owner_id=user.id,
            project=None,
            conversation_id=payload.conversation_id,
            question=_last_chat_question(payload),
            answer_state=state,
            history_messages=messages,
        )
        state["conversation_id"] = conversation.id
        return state
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/chat/{project_id}/ask")
def chat_ask(
    project_id: int,
    payload: ChatAskRequest,
    db: Session = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    project = project_or_404(db, user, project_id)
    messages = [message.model_dump(mode="json") for message in payload.messages]
    try:
        state = chat_with_corpus(db, project, messages)
        conversation = persist_chat_exchange(
            db,
            owner_id=user.id,
            project=project,
            conversation_id=payload.conversation_id,
            question=_last_chat_question(payload),
            answer_state=state,
            history_messages=messages,
        )
        state["conversation_id"] = conversation.id
        return state
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

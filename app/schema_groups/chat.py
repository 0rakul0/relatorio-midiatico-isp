from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

class ChatTurnIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)

class ChatAskRequest(BaseModel):
    messages: list[ChatTurnIn] = Field(min_length=1, max_length=50)
    conversation_id: int | None = Field(default=None, ge=1)

class ChatResponse(StrictLLMOutput):
    """Resposta do chat com rastreabilidade de corpus e pesquisa externa."""

    answer: str = Field(min_length=1, max_length=12000)
    used_member_indices: list[int] = Field(default_factory=list, max_length=60)
    used_external_urls: list[str] = Field(default_factory=list, max_length=30)

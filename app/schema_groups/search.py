from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

class AgentWebSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Web search query")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum results")

class AgentSearchHit(StrictLLMOutput):
    title: str
    url: str
    media_origin: Literal["PORTAL_NOTICIAS", "REDE_SOCIAL", "YOUTUBE"] | None = None
    snippet: str | None = None
    content: str | None = None
    published_at: str | None = None
    source_name: str | None = None
    view_count: int | None = Field(default=None, ge=0)
    provider: str
    source_index: int | None = Field(default=None, ge=0)

class AgentSearchResponse(StrictLLMOutput):
    query: str
    provider: str
    status: Literal["OK", "NO_RESULTS", "ERROR"]
    results: list[AgentSearchHit] = Field(default_factory=list, max_length=10)

class AgentBulkSearchArgs(BaseModel):
    queries: list[str] = Field(
        min_length=1,
        max_length=50,
        description="Already-approved queries to execute in bulk without rewriting them",
    )

class AgentBulkQueryResult(StrictLLMOutput):
    query: str
    provider: str
    status: Literal["OK", "NO_RESULTS", "ERROR", "SKIPPED"]
    error: str | None = None
    returned: int = Field(default=0, ge=0)
    accepted: int = Field(default=0, ge=0)
    hits: list[AgentSearchHit] = Field(default_factory=list, max_length=10)

class AgentBulkSearchResponse(StrictLLMOutput):
    results: list[AgentBulkQueryResult] = Field(default_factory=list, max_length=50)
    cancelled: bool = False

class CollectorExecutionResponse(StrictLLMOutput):
    status: Literal["COMPLETED", "PARTIAL", "UNAVAILABLE"]
    detail: str
    portal_hits: int = Field(default=0, ge=0)
    youtube_hits: int = Field(default=0, ge=0)
    social_hits: int = Field(default=0, ge=0)

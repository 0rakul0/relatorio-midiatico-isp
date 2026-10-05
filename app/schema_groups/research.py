from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

class AgentAcademicSearchArgs(BaseModel):
    queries: list[str] = Field(
        min_length=1,
        max_length=3,
        description="One to three scientific literature queries for multiple academic databases",
    )
    max_results_per_query: int = Field(default=6, ge=1, le=10)

class AcademicToolPaper(StrictLLMOutput):
    provider: Literal["scielo", "openalex", "crossref", "semantic_scholar", "arxiv"]
    external_id: str
    arxiv_id: str | None = None
    doi: str | None = None
    title: str
    authors: list[str] = Field(default_factory=list, max_length=50)
    abstract: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    categories: list[str] = Field(default_factory=list, max_length=30)
    url: str
    pdf_url: str | None = None
    journal_reference: str | None = None
    is_preprint: bool = True

class AcademicSearchToolResponse(StrictLLMOutput):
    queries: list[str] = Field(default_factory=list, max_length=3)
    provider: Literal["multi"]
    status: Literal["OK", "NO_RESULTS", "ERROR"]
    results: list[AcademicToolPaper] = Field(default_factory=list, max_length=20)
    errors: list[str] = Field(default_factory=list, max_length=10)

class AcademicPaperSelection(StrictLLMOutput):
    provider: Literal["scielo", "openalex", "crossref", "semantic_scholar", "arxiv"]
    external_id: str

    # Deve reproduzir o titulo original retornado pela tool. Os campos pt-BR
    # sao a camada de apresentacao/traducao e nunca substituem a fonte original.
    title: str
    title_ptbr: str = Field(min_length=1, max_length=2000)
    abstract_ptbr: str | None = Field(default=None, max_length=12000)
    original_language: str | None = Field(default=None, max_length=20)

    relevance_score: float = Field(ge=0, le=1)
    relation_to_topic: str = Field(min_length=3, max_length=4000)

class AcademicResearchResponse(StrictLLMOutput):
    searched: bool
    queries: list[str] = Field(default_factory=list, max_length=3)
    papers: list[AcademicPaperSelection] = Field(default_factory=list, max_length=10)
    summary: str | None = Field(default=None, max_length=5000)

class AgentBulkArticleFetchArgs(BaseModel):
    urls: list[str] = Field(
        min_length=1,
        max_length=100,
        description="Canonical article URLs selected for full-text hydration",
    )

class ArticleFetchItemResult(StrictLLMOutput):
    url: str
    status: Literal["FETCHED", "EMPTY_OR_BLOCKED", "ERROR"]
    chars: int = Field(default=0, ge=0)
    error: str | None = None

class ArticleFetchToolResponse(StrictLLMOutput):
    results: list[ArticleFetchItemResult] = Field(default_factory=list, max_length=100)

class HydrationExecutionResponse(StrictLLMOutput):
    status: Literal["COMPLETED", "PARTIAL", "UNAVAILABLE"]
    detail: str


# ---------------------------------------------------------------------------
# Topic profile
# ---------------------------------------------------------------------------

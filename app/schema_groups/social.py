from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

class SocialCommentAssessment(StrictLLMOutput):
    index: int = Field(ge=0)
    sentiment: Literal["POSITIVO", "NEGATIVO", "NEUTRO", "AMBIGUO"]
    emotion: Literal[
        "MEDO", "INDIGNACAO", "CONFIANCA", "DESCONFIANCA", "TRISTEZA",
        "IRONIA", "ESPERANCA", "OUTRA", "NAO_IDENTIFICAVEL",
    ]
    position: Literal[
        "APOIO", "CRITICA", "PREOCUPACAO", "DUVIDA", "RELATO_PESSOAL",
        "OUTRA", "NAO_IDENTIFICAVEL",
    ]
    themes: list[str] = Field(default_factory=list, max_length=4)

class SocialCommentBatchResponse(StrictLLMOutput):
    assessments: list[SocialCommentAssessment] = Field(
        default_factory=list, max_length=60
    )

class SocialNarrativeOutput(StrictLLMOutput):
    title: str = Field(min_length=3, max_length=200)
    analysis: str = Field(min_length=10, max_length=2500)

class SocialDiscourseAnalysisResponse(StrictLLMOutput):
    overall_reading: str = Field(min_length=40, max_length=5000)
    dominant_narratives: list[SocialNarrativeOutput] = Field(default_factory=list, max_length=8)
    recurring_arguments: list[SocialNarrativeOutput] = Field(default_factory=list, max_length=8)
    tensions_and_contradictions: list[SocialNarrativeOutput] = Field(default_factory=list, max_length=8)
    interaction_patterns: list[SocialNarrativeOutput] = Field(default_factory=list, max_length=8)
    polarization_signals: str = Field(min_length=20, max_length=4000)
    sample_limitations: str = Field(min_length=20, max_length=2500)

class PublicOpinionIndicatorOutput(StrictLLMOutput):
    label: str
    question: str | None
    value: str
    unit: str | None
    subgroup: str | None
    evidence: str

class PublicOpinionSurveyExtractionResponse(StrictLLMOutput):
    is_public_opinion_research: bool
    institute: str | None
    sponsor: str | None
    population: str | None
    geography: str | None
    field_start: str | None
    field_end: str | None
    publication_date: str | None
    sample_size: int | None = Field(default=None, ge=1)
    margin_of_error: str | None
    confidence_level: str | None
    methodology: str | None
    sampling_method: str | None
    representative_scope: str | None
    caveats: str | None
    indicators: list[PublicOpinionIndicatorOutput] = Field(default_factory=list, max_length=20)
    evidence_summary: str | None


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

class TopicRuleSet(StrictLLMOutput):
    professional_status: list[str] = Field(max_length=20)
    notes: list[str] = Field(max_length=20)

class TopicProfileResponse(StrictLLMOutput):
    project_type: Literal["INSTITUTIONAL_PRODUCT", "EVENT_TOPIC", "GENERAL_TOPIC"]
    product_name: str | None
    product_anchor: str | None
    product_search_variants: list[str] = Field(max_length=10)
    subject_terms: list[str] = Field(max_length=20)
    event_type: str
    event_anchor: str | None
    event_search_variants: list[str] = Field(max_length=12)
    fact_discovery_variants: list[str] = Field(max_length=16)
    actors: list[str] = Field(max_length=20)
    actions: list[str] = Field(max_length=20)
    locations: list[str] = Field(max_length=20)
    organizations: list[str] = Field(max_length=20)
    search_synonyms: list[str] = Field(max_length=30)
    requested_fact_fields: list[str] = Field(max_length=30)
    inclusion_rules: TopicRuleSet
    exclusion_rules: TopicRuleSet


# ---------------------------------------------------------------------------
# Institutional product discovery
# ---------------------------------------------------------------------------

class OfficialFactOutput(StrictLLMOutput):
    label: str
    value: str
    evidence: str
    source_index: int = Field(ge=0)
    indicator: str | None
    geography: str | None
    period_start: str | None
    period_end: str | None
    unit: str | None

class InstitutionalProductProfileResponse(StrictLLMOutput):
    institution: str | None
    product_status: Literal["PUBLISHED", "ANNOUNCED", "NOT_CONFIRMED"]
    product_evidence: str | None
    product_source_index: int | None = Field(ge=0)
    launch_status: Literal["CONFIRMED_ACTUAL", "EXPECTED_ONLY", "NOT_FOUND"]
    launch_date: str | None
    expected_launch_date: str | None
    launch_evidence: str | None
    launch_source_index: int | None = Field(ge=0)
    official_facts: list[OfficialFactOutput] = Field(max_length=20)


# ---------------------------------------------------------------------------
# Search planning
# ---------------------------------------------------------------------------

class SearchStrategyResponse(StrictLLMOutput):
    """A compact search strategy, not a long list of paraphrased queries."""

    primary_query: str = Field(min_length=3, max_length=500)
    complementary_queries: list[str] = Field(default_factory=list, max_length=2)
    fact_query: str | None = Field(default=None, max_length=500)
    official_query: str | None = Field(default=None, max_length=500)
    rationale: str = Field(min_length=3, max_length=2000)

class ProcessDecision(StrictLLMOutput):
    enabled: bool
    reason: str = Field(min_length=3, max_length=1000)

class ReportPlanResponse(StrictLLMOutput):
    """Methodological plan + compact search strategy for one report run."""

    web_collection: ProcessDecision
    youtube_collection: ProcessDecision
    social_repercussion: ProcessDecision | None = None
    academic_research: ProcessDecision
    media_validation: ProcessDecision
    fact_extraction: ProcessDecision
    fact_resolution: ProcessDecision
    nominal_followup: ProcessDecision
    second_fact_pass: ProcessDecision
    classification: ProcessDecision
    report_writer: ProcessDecision
    qa: ProcessDecision

    fact_fields: list[str] = Field(default_factory=list, max_length=30)

    primary_query: str = Field(min_length=3, max_length=500)
    complementary_queries: list[str] = Field(default_factory=list, max_length=2)
    fact_query: str | None = Field(default=None, max_length=500)
    official_query: str | None = Field(default=None, max_length=500)
    rationale: str = Field(min_length=3, max_length=3000)


# Legacy schema kept for compatibility with old stored code/tests. New planning
# uses SearchStrategyResponse.

class SearchPlanItem(StrictLLMOutput):
    query: str
    kind: str
    purpose: Literal["MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"]
    rationale: str
    priority: int = Field(ge=1, le=3)

class SearchPlanResponse(StrictLLMOutput):
    queries: list[SearchPlanItem] = Field(max_length=50)


# ---------------------------------------------------------------------------
# Cobertura complementar (fase 2: lacunas -> planejador -> coleta direcionada)
# ---------------------------------------------------------------------------

class GapFillQuery(StrictLLMOutput):
    query: str
    focus: str
    rationale: str

class GapFillResponse(StrictLLMOutput):
    queries: list[GapFillQuery] = Field(default_factory=list, max_length=12)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

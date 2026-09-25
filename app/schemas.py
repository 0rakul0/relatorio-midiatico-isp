from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# ---------------------------------------------------------------------------
# API input schemas
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=300)
    institution: str = "Instituto de Segurança Pública"
    launch_date: date | None = None
    collection_start: date | None = None
    collection_end: date | None = None
    event_start: date | None = None
    event_end: date | None = None

    execution_profile: Literal[
        "AUTO",
        "MIDIATICO_SIMPLES",
        "MIDIATICO_COM_FATOS",
        "COMPLETO_NOMINAL",
    ] = "AUTO"

    enable_youtube: bool | None = None
    enable_fact_layer: bool | None = None
    enable_nominal_followup: bool | None = None
    enable_academic_research: bool | None = None
    enable_social_repercussion: bool | None = None


class OfficialFactCreate(BaseModel):
    label: str
    value: str
    source_reference: str
    page: int | None = None
    evidence: str
    indicator: str | None = None
    geography: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    unit: str | None = None


class ManualMediaItemCreate(BaseModel):
    title: str
    url: HttpUrl
    published_at: date | None = None
    snippet: str | None = None
    content: str | None = None
    source_name: str | None = None
    view_count: int | None = Field(default=None, ge=0)
    query_id: int | None = None
    purpose: Literal[
        "MEDIA_REPERCUSSION",
        "FACT_DISCOVERY",
        "OFFICIAL_FACT",
        "NOMINAL_FOLLOWUP",
    ] = "MEDIA_REPERCUSSION"


# ---------------------------------------------------------------------------
# Strict base for structured LLM outputs
# ---------------------------------------------------------------------------


class StrictLLMOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Optional agent-tool contracts
# ---------------------------------------------------------------------------


class AgentWebSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Web search query")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum results")


class AgentVideoSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Video search query")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum results")


class AgentSearchHit(StrictLLMOutput):
    title: str
    url: str
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


class FactFieldOutput(StrictLLMOutput):
    value: str | None
    evidence: str | None
    basis: Literal["EXPLICIT", "RELATIVE_TO_PUBLICATION", "NOT_PRESENT"]


class FactEventOutput(StrictLLMOutput):
    related_to_topic: bool
    event_type: str
    subject_type: str | None
    relation_reason: str
    subject_name: FactFieldOutput
    institution: FactFieldOutput
    rank_or_role: FactFieldOutput
    unit: FactFieldOutput
    professional_status: FactFieldOutput
    event_date: FactFieldOutput
    death_date: FactFieldOutput
    operation_name: FactFieldOutput
    death_count: FactFieldOutput
    arrest_count: FactFieldOutput
    weapon_count: FactFieldOutput
    rifle_count: FactFieldOutput
    cause_category: FactFieldOutput
    cause_description: FactFieldOutput
    circumstance: FactFieldOutput
    address: FactFieldOutput
    neighborhood: FactFieldOutput
    city: FactFieldOutput
    state: FactFieldOutput
    death_place_name: FactFieldOutput
    death_address: FactFieldOutput
    death_neighborhood: FactFieldOutput
    death_city: FactFieldOutput
    death_state: FactFieldOutput


class FactExtractionResponse(StrictLLMOutput):
    events: list[FactEventOutput] = Field(max_length=10)


# ---------------------------------------------------------------------------
# Media validation and classification
# ---------------------------------------------------------------------------


class YouTubeCrossValidationResponse(StrictLLMOutput):
    status: Literal[
        "CONFIRMED",
        "PARTIALLY_CONFIRMED",
        "CONFLICT",
        "INSUFFICIENT_EVIDENCE",
    ]
    matching_fields: list[str]
    conflicting_fields: list[str]
    detail: str


class MediaRelevanceDecision(StrictLLMOutput):
    media_item_id: int
    related: bool
    relation_type: Literal[
        "DIRECT_PRODUCT",
        "ATTRIBUTED_FINDING",
        "DERIVED_COVERAGE",
        "DIRECT_EVENT",
        "THEMATIC_CONTEXT",
        "THEMATIC_ONLY",
        "UNRELATED",
    ]
    anchor: str
    evidence: str
    reason: str


class MediaRelevanceBatchResponse(StrictLLMOutput):
    decisions: list[MediaRelevanceDecision] = Field(max_length=40)


class MediaClassificationOutput(StrictLLMOutput):
    media_item_id: int
    theme: str
    framing: str
    isp_mentioned: bool
    tone_toward_institution: Literal["POSITIVO", "NEUTRO", "NEGATIVO", "INVERIFICÁVEL"]
    fidelity_status: Literal["FIEL", "DIVERGENTE", "INVERIFICÁVEL"]
    evidence: str
    errors: list[str]


class MediaClassificationBatchResponse(StrictLLMOutput):
    classifications: list[MediaClassificationOutput] = Field(max_length=40)


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------


class ThematicAxisOutput(StrictLLMOutput):
    axis: str
    anchor_data: str
    coverage: str


class RiskAssessmentOutput(StrictLLMOutput):
    dimension: str
    assessment: str
    evidence: str


class PressKitOutput(StrictLLMOutput):
    product: str
    purpose: str


class StructuredMediaReportResponse(StrictLLMOutput):
    title: str
    interpretive_title: str
    subtitle: str
    executive_summary: str
    fact_layer_intro: str
    opening: str
    panorama: str
    dominant_framing: str
    highest_yield: str
    institutional_narrative: str
    synthesis: str
    methodological_note: str
    thematic_axes: list[ThematicAxisOutput] = Field(max_length=10)
    risk_assessment: list[RiskAssessmentOutput] = Field(max_length=8)
    recommendations: list[str] = Field(max_length=10)
    press_kit: list[PressKitOutput] = Field(max_length=10)


# ---------------------------------------------------------------------------
# Final QA
# ---------------------------------------------------------------------------


class QAFindingOutput(StrictLLMOutput):
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    code: str
    message: str


class ReportQAResponse(StrictLLMOutput):
    findings: list[QAFindingOutput] = Field(max_length=30)


# ---------------------------------------------------------------------------
# Chat com o corpus coletado
# ---------------------------------------------------------------------------


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

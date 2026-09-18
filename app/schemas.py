from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# ---------------------------------------------------------------------------
# Schemas de entrada da API
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
    enable_cross_validation: bool | None = None


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
    query_id: int | None = None
    purpose: Literal[
        "MEDIA_REPERCUSSION",
        "FACT_DISCOVERY",
        "OFFICIAL_FACT",
        "NOMINAL_FOLLOWUP",
    ] = "MEDIA_REPERCUSSION"


# ---------------------------------------------------------------------------
# Base estrita para todas as saídas estruturadas da LLM
# ---------------------------------------------------------------------------


class StrictLLMOutput(BaseModel):
    """Base comum das respostas estruturadas geradas pela LLM.

    ``extra='forbid'`` produz ``additionalProperties: false`` no JSON Schema e
    também impede que uma resposta com campos inesperados seja aceita depois
    da chamada. Assim o mesmo contrato vale na geração e na validação local.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Contratos das tools opcionais do agente
# ---------------------------------------------------------------------------


class AgentWebSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Consulta que deve ser pesquisada na internet")
    max_results: int = Field(default=5, ge=1, le=10, description="Quantidade máxima de resultados")


class AgentVideoSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500, description="Consulta para localizar vídeos relevantes")
    max_results: int = Field(default=5, ge=1, le=10, description="Quantidade máxima de resultados")


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
        description="Consultas já planejadas que devem ser executadas em lote, sem alteração",
    )


class AgentBulkQueryResult(StrictLLMOutput):
    query: str
    provider: str
    status: Literal["OK", "NO_RESULTS", "ERROR", "SKIPPED"]
    error: str | None = None
    hits: list[AgentSearchHit] = Field(default_factory=list, max_length=10)


class AgentBulkSearchResponse(StrictLLMOutput):
    results: list[AgentBulkQueryResult] = Field(default_factory=list, max_length=50)
    cancelled: bool = False


class CollectorExecutionResponse(StrictLLMOutput):
    status: Literal["COMPLETED", "PARTIAL", "UNAVAILABLE"]
    detail: str


# ---------------------------------------------------------------------------
# Perfil do tema
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
# Descoberta documental de produto institucional
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
# Planejamento de busca
# ---------------------------------------------------------------------------


class SearchPlanItem(StrictLLMOutput):
    query: str
    kind: str
    purpose: Literal["MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"]
    rationale: str
    priority: int = Field(ge=1, le=3)


class SearchPlanResponse(StrictLLMOutput):
    # O limite operacional final continua sendo aplicado por settings no caller.
    queries: list[SearchPlanItem] = Field(max_length=50)


# ---------------------------------------------------------------------------
# Extração factual
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
# Validação de mídia e classificação
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
        "DIRECT_EVENT",
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
# Relatório final
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
# QA final
# ---------------------------------------------------------------------------


class QAFindingOutput(StrictLLMOutput):
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    code: str
    message: str


class ReportQAResponse(StrictLLMOutput):
    findings: list[QAFindingOutput] = Field(max_length=30)

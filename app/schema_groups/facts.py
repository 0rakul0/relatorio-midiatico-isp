from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

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
# Public opinion research
# ---------------------------------------------------------------------------

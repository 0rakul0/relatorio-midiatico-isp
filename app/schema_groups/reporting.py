from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schema_groups.base import StrictLLMOutput

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
    public_opinion_summary: str
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

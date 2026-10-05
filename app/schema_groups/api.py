from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=300)
    geographic_scopes: list[str] = Field(default_factory=list, max_length=27)
    # Compatibilidade com clientes que ainda enviam um único estado.
    geographic_scope: str | None = Field(default=None, max_length=60)
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
    enable_fact_layer: bool | None = None
    enable_nominal_followup: bool | None = None
    enable_academic_research: bool | None = None

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

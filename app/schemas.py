from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=300)
    institution: str = "Instituto de Segurança Pública"
    launch_date: date | None = None
    collection_start: date | None = None
    collection_end: date | None = None
    event_start: date | None = None
    event_end: date | None = None

    # AUTO decide o pipeline depois que o tema é classificado.
    # Os demais perfis permitem forçar um comportamento específico.
    execution_profile: Literal[
        "AUTO",
        "MIDIATICO_SIMPLES",
        "MIDIATICO_COM_FATOS",
        "COMPLETO_NOMINAL",
    ] = "AUTO"

    # Overrides opcionais. Quando ficam como None, o perfil decide.
    enable_youtube: bool | None = None
    enable_fact_layer: bool | None = None
    enable_nominal_followup: bool | None = None
    enable_cross_validation: bool | None = None


class LLMSettingsUpdate(BaseModel):
    # provider permanece opcional apenas para compatibilidade com clientes antigos.
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=1, max_length=120)


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
    purpose: Literal["MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT", "NOMINAL_FOLLOWUP"] = "MEDIA_REPERCUSSION"

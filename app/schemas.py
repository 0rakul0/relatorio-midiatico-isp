from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl


class ProjectCreate(BaseModel):
    topic: str = Field(min_length=3, max_length=300)
    institution: str = "Instituto de Segurança Pública"
    launch_date: date | None = None
    collection_start: date | None = None
    collection_end: date | None = None


class LLMSettingsUpdate(BaseModel):
    provider: Literal["openai", "groq"]
    model: str = Field(min_length=1, max_length=120)


class OfficialFactCreate(BaseModel):
    label: str
    value: str
    source_reference: str
    page: int | None = None
    evidence: str


class ManualMediaItemCreate(BaseModel):
    title: str
    url: HttpUrl
    published_at: date | None = None
    snippet: str | None = None
    content: str | None = None
    query_id: int | None = None


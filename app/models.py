from datetime import date, datetime
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(300))
    institution: Mapped[str] = mapped_column(String(200), default="Instituto de Segurança Pública")
    launch_date: Mapped[date] = mapped_column(Date)
    collection_start: Mapped[date] = mapped_column(Date)
    collection_end: Mapped[date] = mapped_column(Date)
    has_custom_date_window: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OfficialFact(Base):
    __tablename__ = "official_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(250))
    value: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str] = mapped_column(String(500))
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[str] = mapped_column(Text)


class SearchQuery(Base):
    __tablename__ = "search_queries"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(40))
    rationale: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (UniqueConstraint("project_id", "canonical_url", name="uq_project_canonical_url"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("search_queries.id"), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(300), nullable=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_source: Mapped[str] = mapped_column(String(50), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    discard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Classification(Base):
    __tablename__ = "classifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), unique=True)
    theme: Mapped[str] = mapped_column(String(150))
    framing: Mapped[str] = mapped_column(String(300))
    isp_mentioned: Mapped[bool] = mapped_column(default=False)
    tone_toward_institution: Mapped[str] = mapped_column(String(30), default="NEUTRO")
    fidelity_status: Mapped[str] = mapped_column(String(30), default="PENDENTE")
    evidence: Mapped[str] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSON, default=list)


class GeneratedReport(Base):
    __tablename__ = "generated_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True)
    body: Mapped[dict] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


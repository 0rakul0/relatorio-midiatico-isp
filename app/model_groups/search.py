from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(50), default="MEDIA_REPERCUSSION")
    rationale: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Estado auditavel da tentativa de pesquisa.
    execution_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    execution_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    providers_attempted: Mapped[list] = mapped_column(JSON, default=list)
    results_returned: Mapped[int] = mapped_column(Integer, default=0)
    results_accepted: Mapped[int] = mapped_column(Integer, default=0)


class SearchCall(Base):
    """Auditoria de cada tentativa externa por consulta e provedor."""

    __tablename__ = "search_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    search_query_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_queries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(60))
    provider: Mapped[str] = mapped_column(String(40))
    query: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    results_returned: Mapped[int] = mapped_column(Integer, default=0)
    results_accepted: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SearchHit(Base):
    """Resultado bruto preservado exatamente no ponto de coleta.

    Um hit nunca e apagado apenas por ser irrelevante, estar fora da janela,
    divergir do dominio/canal alvo ou repetir uma URL ja encontrada. Essas
    situacoes sao registradas em ``technical_flags`` e resolvidas nas camadas
    posteriores. ``MediaItem`` continua sendo a entidade consolidada por URL.
    """

    __tablename__ = "search_hits"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    search_query_id: Mapped[int | None] = mapped_column(
        ForeignKey("search_queries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True
    )

    provider: Mapped[str] = mapped_column(String(40), index=True)
    purpose: Mapped[str] = mapped_column(String(50), default="MEDIA_REPERCUSSION")
    # Origem do link: PORTAL_NOTICIAS | REDE_SOCIAL | YOUTUBE.
    media_origin: Mapped[str] = mapped_column(String(30), default="PORTAL_NOTICIAS")
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    target: Mapped[str | None] = mapped_column(String(300), nullable=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(300), nullable=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    published_at_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    technical_status: Mapped[str] = mapped_column(String(40), default="COLLECTED")
    technical_flags: Mapped[list] = mapped_column(JSON, default=list)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )

from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CorpusDocument(Base):
    """Global article snapshot reusable across report projects."""

    __tablename__ = "corpus_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_source: Mapped[str] = mapped_column(String(50), default="unknown")
    source_provenance: Mapped[list] = mapped_column(JSON, default=list)

    # Metadados de memória global. content_hash permite deduplicar a mesma
    # matéria encontrada por URLs diferentes; embedding é reutilizado entre
    # projetos e nunca substitui a validação semântica final.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    alternate_urls: Mapped[list] = mapped_column(JSON, default=list)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Origem do link: PORTAL_NOTICIAS | REDE_SOCIAL | YOUTUBE.
    media_origin: Mapped[str] = mapped_column(String(30), default="PORTAL_NOTICIAS")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )


class ProjectCorpusLink(Base):
    """How one global document relates to one report project."""

    __tablename__ = "project_corpus_links"
    __table_args__ = (
        UniqueConstraint("project_id", "document_id", name="uq_project_corpus_document"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("corpus_documents.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    origin: Mapped[str] = mapped_column(String(30), default="SEARCH")
    source_project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    relation_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    relation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    relevance_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reranker_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )


class RelevanceTrainingExample(Base):
    """Exemplo supervisionado derivado da validação auditável do corpus."""

    __tablename__ = "relevance_training_examples"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "media_item_id",
            name="uq_relevance_training_project_item",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    corpus_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("corpus_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    query_text: Mapped[str] = mapped_column(Text)
    document_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_text: Mapped[str] = mapped_column(Text)
    label: Mapped[int] = mapped_column(Integer, index=True)
    relation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    decision_source: Mapped[str] = mapped_column(String(80), default="semantic_validation")
    decision_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (UniqueConstraint("project_id", "canonical_url", name="uq_project_canonical_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("search_queries.id"), nullable=True)
    corpus_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("corpus_documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    corpus_origin: Mapped[str] = mapped_column(String(30), default="SEARCH")

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
    # Origem do link: PORTAL_NOTICIAS | REDE_SOCIAL | YOUTUBE.
    media_origin: Mapped[str] = mapped_column(String(30), default="PORTAL_NOTICIAS")
    # Metadados mínimos preservados por coletor para validação cruzada.
    source_provenance: Mapped[list] = mapped_column(JSON, default=list)
    cross_validation_status: Mapped[str] = mapped_column(String(40), default="NOT_APPLICABLE")
    cross_validation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Finalidade(s) pelas quais o item foi descoberto. Um mesmo URL pode ser
    # recuperado por busca factual e por busca de repercussão.
    discovery_purposes: Mapped[list] = mapped_column(JSON, default=list)

    # Elegibilidade para análise de repercussão.
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    discard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    relation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    relevance_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Elegibilidade para a camada factual.
    fact_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    fact_discard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_date_hint: Mapped[date | None] = mapped_column(Date, nullable=True)

    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())

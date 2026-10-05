from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FactEvent(Base):
    __tablename__ = "fact_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)

    event_type: Mapped[str] = mapped_column(String(100), default="OTHER")
    operation_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    subject_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    normalized_subject_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    institution: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rank_or_role: Mapped[str | None] = mapped_column(String(200), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(200), nullable=True)
    professional_status: Mapped[str | None] = mapped_column(String(80), nullable=True)

    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    death_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    cause_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cause_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    circumstance: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Local do fato/ataque/ocorrência.
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    neighborhood: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Local da morte, separado do local do fato. Ex.: ataque em um município
    # e óbito posterior em hospital de outro município.
    death_place_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    death_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    death_neighborhood: Mapped[str | None] = mapped_column(String(200), nullable=True)
    death_city: Mapped[str | None] = mapped_column(String(200), nullable=True)
    death_state: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # True = núcleo principal; False = caso relacionado fora do núcleo;
    # None = escopo ainda pendente de revisão.
    primary_scope: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    resolution_status: Mapped[str] = mapped_column(String(50), default="PARTIALLY_CONFIRMED")
    conflict_fields: Mapped[list] = mapped_column(JSON, default=list)
    extra_attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FactAssertion(Base):
    __tablename__ = "fact_assertions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("fact_events.id", ondelete="CASCADE"), index=True)
    media_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True)

    field_name: Mapped[str] = mapped_column(String(100), index=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(50))
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    evidence: Mapped[str] = mapped_column(Text)

    evidence_status: Mapped[str] = mapped_column(String(50), default="SUPPORTED")
    resolution_method: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Data em que a cifra/afirmação foi reportada na fonte. Em balanços de
    # operações, isso permite distinguir atualização temporal de conflito real.
    reported_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OperationEvent(Base):
    """Tabela-mestra de operações policiais identificadas no projeto.

    É uma projeção auditável da camada factual: cada registro aponta para o
    FactEvent que o originou e agrega fontes oficiais, repercussão e balanços.
    """

    __tablename__ = "operation_events"
    __table_args__ = (
        UniqueConstraint("project_id", "fact_event_id", name="uq_operation_project_fact_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    fact_event_id: Mapped[int] = mapped_column(
        ForeignKey("fact_events.id", ondelete="CASCADE"), index=True
    )
    operation_name: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    neighborhoods: Mapped[list] = mapped_column(JSON, default=list)
    forces: Mapped[list] = mapped_column(JSON, default=list)

    resolution_status: Mapped[str] = mapped_column(String(50), default="PARTIALLY_CONFIRMED")
    official_supported: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    official_source_count: Mapped[int] = mapped_column(Integer, default=0)
    media_source_count: Mapped[int] = mapped_column(Integer, default=0)
    repercussion_count: Mapped[int] = mapped_column(Integer, default=0)
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    official_urls: Mapped[list] = mapped_column(JSON, default=list)
    media_urls: Mapped[list] = mapped_column(JSON, default=list)
    count_timelines: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=func.now(),
    )


class OperationMediaLink(Base):
    """Relação auditável operação ↔ item coletado."""

    __tablename__ = "operation_media_links"
    __table_args__ = (
        UniqueConstraint(
            "operation_event_id", "media_item_id",
            name="uq_operation_media_item",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_event_id: Mapped[int] = mapped_column(
        ForeignKey("operation_events.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(40), default="EVIDENCE")
    source_type: Mapped[str] = mapped_column(String(40), default="MEDIA")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), unique=True)
    theme: Mapped[str] = mapped_column(String(150))
    # Enquadramento é texto analítico produzido pela classificação e pode
    # ultrapassar facilmente 300 caracteres. TEXT evita truncamento no
    # PostgreSQL durante classificação inicial ou cobertura complementar.
    framing: Mapped[str] = mapped_column(Text)
    isp_mentioned: Mapped[bool] = mapped_column(default=False)
    tone_toward_institution: Mapped[str] = mapped_column(String(30), default="NEUTRO")
    fidelity_status: Mapped[str] = mapped_column(String(30), default="PENDENTE")
    evidence: Mapped[str] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSON, default=list)

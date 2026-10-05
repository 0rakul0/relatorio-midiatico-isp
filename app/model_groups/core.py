from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Dono (auth.uid do Supabase). NULL = legado, visível só para admin.
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic: Mapped[str] = mapped_column(String(300))
    institution: Mapped[str] = mapped_column(String(200), default="Instituto de Segurança Pública")

    # Mantido por compatibilidade com projetos de produto institucional.
    launch_date: Mapped[date] = mapped_column(Date)

    # Janela da repercussão midiática.
    collection_start: Mapped[date] = mapped_column(Date)
    collection_end: Mapped[date] = mapped_column(Date)

    # Janela do fato. Em temas factuais pode ser igual à janela midiática,
    # mas é conceitualmente independente.
    event_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    project_type: Mapped[str] = mapped_column(String(40), default="AUTO")
    topic_profile: Mapped[dict] = mapped_column(JSON, default=dict)

    # Perfil escolhido pelo usuário. AUTO é resolvido depois que o tema é classificado.
    execution_profile: Mapped[str] = mapped_column(String(40), default="AUTO")
    # Overrides pontuais de agentes, por exemplo {"enable_youtube": False}.
    execution_options: Mapped[dict] = mapped_column(JSON, default=dict)
    # Plano metodologico resolvido para esta pauta. Mantem as decisoes e razoes
    # que habilitam/desabilitam etapas opcionais da pipeline.
    execution_plan: Mapped[dict] = mapped_column(JSON, default=dict)

    fact_grace_days: Mapped[int] = mapped_column(Integer, default=10)

    has_custom_date_window: Mapped[bool] = mapped_column(Boolean, default=False)
    # Resultado mais recente da coleta do YouTube. Permite que uma limitação
    # externa seja registrada sem interromper a geração do relatório.
    youtube_collection_status: Mapped[str] = mapped_column(String(40), default="NOT_ATTEMPTED")
    youtube_collection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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

    # Metadados que impedem misturar números de territórios/períodos diferentes.
    indicator: Mapped[str | None] = mapped_column(String(250), nullable=True)
    geography: Mapped[str | None] = mapped_column(String(200), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AcademicPaper(Base):
    """Scientific literature selected for one report topic.

    Academic papers are contextual evidence and never count as media
    repercussion items.
    """

    __tablename__ = "academic_papers"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "provider", "external_id",
            name="uq_project_academic_paper",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="arxiv")
    external_id: Mapped[str] = mapped_column(String(150))
    arxiv_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Conteudo original retornado pelo provedor. Nunca e sobrescrito pela traducao.
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Versao de apresentacao em portugues do Brasil. A traducao e produzida
    # pelo mesmo ReportAgent que seleciona os artigos, depois do retorno real
    # da tool do arXiv, preservando o original para auditoria.
    title_ptbr: Mapped[str | None] = mapped_column(Text, nullable=True)
    abstract_ptbr: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    authors: Mapped[list] = mapped_column(JSON, default=list)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    url: Mapped[str] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    journal_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_preprint: Mapped[bool] = mapped_column(Boolean, default=True)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    relation_to_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

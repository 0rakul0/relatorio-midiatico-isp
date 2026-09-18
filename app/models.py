from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
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

    # Elegibilidade para a camada factual.
    fact_status: Mapped[str] = mapped_column(String(40), default="PENDING")
    fact_discard_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_date_hint: Mapped[date | None] = mapped_column(Date, nullable=True)

    duplicate_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), server_default=func.now())


class FactEvent(Base):
    __tablename__ = "fact_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)

    event_type: Mapped[str] = mapped_column(String(100), default="OTHER")
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
    qa_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    qa_findings: Mapped[list] = mapped_column(JSON, default=list)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ReportRun(Base):
    """Estado persistido de uma execucao assincrona de relatorio."""

    __tablename__ = "report_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stages: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class LLMCall(Base):
    """Registro de custo/consumo de uma chamada única à OpenAI.

    Cada tentativa HTTP é registrada por linha (inclusive tentativas que
    falharam antes de devolver conteúdo, com tokens zerados), para o monitor
    refletir o custo real de retries e fallbacks.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # Operação de negócio (ex.: "plan_queries", "classify", "draft_report") e,
    # quando disponível, o nome do schema JSON usado pela chamada.
    operation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Função de origem (structured_response), nome exato do modelo chamado e
    # se a chamada produziu conteúdo.
    caller: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str] = mapped_column(String(120))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Consumo reportado pela API.
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    search_calls: Mapped[int] = mapped_column(Integer, default=0)

    # Custo estimado em USD com base na tabela de preços local.
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

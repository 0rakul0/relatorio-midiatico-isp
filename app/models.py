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


class GeneratedReport(Base):
    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True)
    body: Mapped[dict] = mapped_column(JSON)
    qa_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    qa_findings: Mapped[list] = mapped_column(JSON, default=list)
    generated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Ponteiro da versão ativa. O espelho abaixo (body/qa_*) preserva a
    # semântica antiga de "relatório atual" para leitores existentes.
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("report_versions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ReportVersion(Base):
    """Versão imutável de um relatório gerado.

    Cada ``_save_report`` com conteúdo estruturalmente novo cria uma linha nova
    com hash SHA-256 do payload. O QA grava status e achados na versão precisa
    em que foi executado; o apontado em ``generated_reports.current_version_id``
    é apenas o atalho para a versão ativa.
    """

    __tablename__ = "report_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version_no", name="uq_report_versions_project_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    body: Mapped[dict] = mapped_column(JSON)
    qa_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    qa_findings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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


class AppUser(Base):
    """Identidade local espelhada do Supabase Auth (id = auth.uid)."""

    __tablename__ = "app_users"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    plan: Mapped[str] = mapped_column(String(30), default="FREE")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatConversation(Base):
    """Thread persistente do chat associado ao usuário e ao escopo temático."""

    __tablename__ = "chat_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), index=True
    )
    scope_type: Mapped[str] = mapped_column(String(20), index=True)
    scope_key: Mapped[str] = mapped_column(String(500), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(120), default="Nova conversa")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), index=True
    )


class ChatMessage(Base):
    """Mensagem persistida de uma conversa, incluindo fontes exibidas."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Subscription(Base):
    """Assinatura (Mercado Pago; MOCK até a integração real)."""

    __tablename__ = "subscriptions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True
    )
    plan: Mapped[str] = mapped_column(String(30), default="FREE")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    mp_preapproval_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
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

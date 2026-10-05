from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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

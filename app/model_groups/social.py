from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SocialPost(Base):
    """Post social usado como ancora para a coleta separada de comentarios."""

    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "platform", "url",
            name="uq_social_post_project_platform_url",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    media_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform: Mapped[str] = mapped_column(String(20), index=True)
    url: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(250), nullable=True, index=True)
    post_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(250), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class SocialComment(Base):
    """Comentario publico normalizado sem identidade do comentarista."""

    __tablename__ = "social_comments"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "platform", "external_id",
            name="uq_social_comment_project_platform_external",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    social_post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(20), index=True)
    external_id: Mapped[str] = mapped_column(String(250), index=True)
    parent_external_id: Mapped[str | None] = mapped_column(String(250), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_url: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class SocialAnalysis(Base):
    """Agregado auditavel da percepcao observada nos comentarios sociais."""

    __tablename__ = "social_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(40), default="PENDING")
    total_posts: Mapped[int] = mapped_column(Integer, default=0)
    total_comments: Mapped[int] = mapped_column(Integer, default=0)
    analyzed_comments: Mapped[int] = mapped_column(Integer, default=0)
    platform_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    sentiment_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    emotion_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    position_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    themes: Mapped[list] = mapped_column(JSON, default=list)
    discourse_analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    methodology_note: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


class PublicOpinionSurvey(Base):
    """Levantamento de opinião pública separado do corpus jornalístico."""

    __tablename__ = "public_opinion_surveys"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_url",
            name="uq_public_opinion_project_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    institute: Mapped[str] = mapped_column(String(300), index=True)
    sponsor: Mapped[str | None] = mapped_column(String(300), nullable=True)
    population: Mapped[str | None] = mapped_column(Text, nullable=True)
    geography: Mapped[str | None] = mapped_column(String(300), nullable=True)
    field_start: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    field_end: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    publication_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    margin_of_error: Mapped[str | None] = mapped_column(String(150), nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(String(150), nullable=True)
    methodology: Mapped[str | None] = mapped_column(Text, nullable=True)
    sampling_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    representative_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    caveats: Mapped[str | None] = mapped_column(Text, nullable=True)
    indicators: Mapped[list] = mapped_column(JSON, default=list)
    evidence_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(String(300), nullable=True)
    provider: Mapped[str] = mapped_column(String(50), default="duckduckgo_text")
    collected_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

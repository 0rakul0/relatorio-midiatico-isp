from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


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

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.fact_layer import fact_assertions_for_report, fact_events_for_main_report
from app.models import GeneratedReport, Project
from app.services.academic_research import academic_papers_for_project
from app.services.execution_profile import execution_flags
from app.services.project_profile import project_payload
from app.services.metrics import corpus_for_project, metrics, split_corpus, split_corpus_by_origin
from app.services.word_cloud import word_cloud_for_project
from app.report_fingerprint import request_fingerprint


def hydrate_cached_report(db: Session, project: Project, generated: GeneratedReport) -> dict:
    """Devolve o snapshot imutável do relatório salvo no momento da geração.

    Métricas, corpus, fatos e dados do projeto NÃO são recalculados a partir do
    banco atual: o histórico precisa permanecer idêntico ao que foi gerado.
    Apenas chaves ausentes em relatórios legados são preenchidas.
    """
    payload = dict(generated.body or {})
    payload.setdefault("project", project_payload(project, for_report=True))
    if "metrics" not in payload:
        payload["metrics"] = metrics(db, project.id)
    if "corpus" not in payload:
        corpus = corpus_for_project(db, project.id)
        payload["corpus"] = corpus
        payload["traditional_corpus"], payload["social_corpus"] = split_corpus(corpus)
    payload.setdefault("traditional_corpus", [])
    payload.setdefault("social_corpus", [])
    if "corpus_by_origin" not in payload and "corpus" in payload:
        payload["corpus_by_origin"] = split_corpus_by_origin(payload["corpus"])
    _, flags = execution_flags(project)
    if "fact_events" not in payload:
        payload["fact_events"] = (
            fact_events_for_main_report(db, project.id) if flags["enable_fact_layer"] else []
        )
    if "fact_evidence" not in payload:
        payload["fact_evidence"] = (
            fact_assertions_for_report(db, project.id, main_report_only=True)
            if flags["enable_fact_layer"]
            else []
        )
    if "academic_papers" not in payload:
        payload["academic_papers"] = (
            academic_papers_for_project(db, project.id)
            if flags.get("enable_academic_research")
            else []
        )
    if "word_cloud" not in payload:
        payload["word_cloud"] = word_cloud_for_project(db, project.id)
    payload["qa"] = {"status": generated.qa_status, "findings": generated.qa_findings or []}
    payload["version_no"] = generated.version_no or 1
    payload["content_hash"] = generated.content_hash
    payload["request_fingerprint"] = generated.request_fingerprint
    payload["cached_at"] = generated.generated_at.isoformat() if generated.generated_at else None
    return payload


def cached_report_for_topic(
    db: Session,
    topic: str,
    collection_start: date | None = None,
    collection_end: date | None = None,
    owner_id: str | None = None,
) -> dict | None:
    statement = (
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(func.lower(Project.topic) == topic.strip().casefold())
        .order_by(GeneratedReport.generated_at.desc())
    )
    if owner_id is not None:
        statement = statement.where(Project.owner_id == owner_id)
    if collection_start:
        statement = statement.where(Project.collection_start == collection_start)
    if collection_end:
        statement = statement.where(Project.collection_end == collection_end)
    row = db.execute(statement).first()
    if not row:
        return None
    project, generated = row
    if (
        generated.request_fingerprint
        and request_fingerprint(project) != generated.request_fingerprint
    ):
        # O estado de entrada (janela, perfil, opções) mudou desde a geração:
        # servir esse snapshot seria enganoso. Força uma nova geração.
        return None
    return hydrate_cached_report(db, project, generated)


def cached_report_for_project(db: Session, project_id: int) -> dict | None:
    row = db.execute(
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(Project.id == project_id)
    ).first()
    return hydrate_cached_report(db, *row) if row else None



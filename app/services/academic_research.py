"""Pesquisa e persistencia da literatura cientifica relacionada a uma pauta."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.models import AcademicPaper, Project
from app.schemas import AcademicResearchResponse
from app.services.project_profile import project_payload
from app.tools.registry import build_agent_tools


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def academic_papers_for_project(db: Session, project_id: int) -> list[dict]:
    rows = db.scalars(
        select(AcademicPaper)
        .where(AcademicPaper.project_id == project_id)
        .order_by(AcademicPaper.relevance_score.desc(), AcademicPaper.published_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "provider": row.provider,
            "external_id": row.external_id,
            "arxiv_id": row.arxiv_id,
            "doi": row.doi,
            # Campos de apresentacao usam pt-BR quando disponivel, enquanto os
            # originais continuam expostos separadamente para auditoria.
            "title": row.title_ptbr or row.title,
            "title_ptbr": row.title_ptbr,
            "title_original": row.title,
            "authors": list(row.authors or []),
            "abstract": row.abstract_ptbr or row.abstract,
            "abstract_ptbr": row.abstract_ptbr,
            "abstract_original": row.abstract,
            "original_language": row.original_language,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "categories": list(row.categories or []),
            "url": row.url,
            "pdf_url": row.pdf_url,
            "journal_reference": row.journal_reference,
            "is_preprint": bool(row.is_preprint),
            "relevance_score": row.relevance_score,
            "relation_to_topic": row.relation_to_topic,
        }
        for row in rows
    ]


def research_academic_literature(
    db: Session,
    project: Project,
    *,
    cancel_check=None,
    progress_detail=None,
) -> dict:
    """Executa a task academica com tool calling e persiste somente resultados reais."""

    if cancel_check:
        cancel_check()

    captured: dict[tuple[str, str], dict] = {}

    def sink(rows: list[dict], provider: str) -> None:
        for row in rows:
            external_id = str(row.get("external_id") or "").strip()
            if external_id:
                captured[(provider, external_id)] = dict(row)

    if progress_detail:
        progress_detail("Consultando literatura cientifica relacionada no arXiv")

    tools = build_agent_tools(enable_academic=True, academic_sink=sink)
    result = get_report_agent().run(
        task="academic_research",
        payload={
            "project": project_payload(project, for_report=True),
            "topic_profile": project.topic_profile or {},
            "instruction": (
                "Busque literatura que ajude a contextualizar cientificamente o tema. "
                "Nao trate artigo academico como item de repercussao midiática."
            ),
        },
        response_model=AcademicResearchResponse,
        schema_name="academic_research_v1",
        tools=tools,
        # A resposta inclui a traducao integral dos abstracts selecionados.
        # Reserva folga para ate 10 artigos sem forcar resumos/truncamentos.
        max_output_tokens=9000,
        max_tool_rounds=1,
    )

    if cancel_check:
        cancel_check()

    persisted = 0
    selected = 0
    for paper in result.get("papers") or []:
        key = (str(paper.get("provider") or "arxiv"), str(paper.get("external_id") or ""))
        raw = captured.get(key)
        if raw is None:
            # Guardrail: nunca persiste artigo que a LLM citou mas a tool nao retornou.
            continue

        selected += 1
        existing = db.scalar(
            select(AcademicPaper).where(
                AcademicPaper.project_id == project.id,
                AcademicPaper.provider == key[0],
                AcademicPaper.external_id == key[1],
            )
        )
        row = existing or AcademicPaper(
            project_id=project.id,
            provider=key[0],
            external_id=key[1],
        )
        row.arxiv_id = raw.get("arxiv_id")
        row.doi = raw.get("doi")
        # A fonte original vem exclusivamente da tool; a LLM so fornece a
        # camada de traducao e a avaliacao de relevancia.
        row.title = raw.get("title") or "Sem titulo"
        row.abstract = raw.get("abstract")
        row.title_ptbr = str(paper.get("title_ptbr") or "").strip() or row.title
        translated_abstract = paper.get("abstract_ptbr")
        row.abstract_ptbr = (
            str(translated_abstract).strip()
            if translated_abstract is not None and str(translated_abstract).strip()
            else None
        )
        row.original_language = (
            str(paper.get("original_language") or "").strip()[:20] or None
        )
        row.authors = list(raw.get("authors") or [])
        row.published_at = _as_date(raw.get("published_at"))
        row.updated_at = _as_date(raw.get("updated_at"))
        row.categories = list(raw.get("categories") or [])
        row.url = raw.get("url")
        row.pdf_url = raw.get("pdf_url")
        row.journal_reference = raw.get("journal_reference")
        row.is_preprint = bool(raw.get("is_preprint", True))
        row.relevance_score = float(paper.get("relevance_score") or 0.0)
        row.relation_to_topic = str(paper.get("relation_to_topic") or "")[:4000] or None
        if existing is None:
            db.add(row)
        persisted += 1

    db.commit()
    papers = academic_papers_for_project(db, project.id)

    return {
        "searched": bool(result.get("searched")),
        "queries": list(result.get("queries") or []),
        "summary": result.get("summary"),
        "candidates_returned": len(captured),
        "selected": selected,
        "persisted": persisted,
        "papers": papers,
    }

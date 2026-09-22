from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.fact_layer import fact_assertions_for_report, fact_events_for_main_report, operation_events_for_report
from app.llm import llm_is_configured
from app.models import GeneratedReport, OfficialFact, Project, ReportVersion
from app.pdf_report import build_pdf
from app.report_fingerprint import content_hash, request_fingerprint
from app.report_qa import run_report_qa
from app.schemas import StructuredMediaReportResponse
from app.services.academic_research import academic_papers_for_project
from app.services.execution_profile import execution_flags
from app.services.project_profile import project_payload
from app.services.metrics import corpus_for_project, metrics, split_corpus, split_corpus_by_origin
from app.services.word_cloud import word_cloud_for_project
from app.services.cache import hydrate_cached_report


def _writer_grounding(db: Session, project: Project) -> dict:
    """Dados de fundamentação compartilhados por redação e revisão."""
    data = metrics(db, project.id)
    corpus = corpus_for_project(db, project.id)
    official_facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    execution_profile, flags = execution_flags(project)
    fact_events = fact_events_for_main_report(db, project.id) if flags["enable_fact_layer"] else []
    operation_events = operation_events_for_report(db, project.id) if flags["enable_fact_layer"] else []
    fact_evidence = (
        fact_assertions_for_report(db, project.id, main_report_only=True)
        if flags["enable_fact_layer"]
        else []
    )
    academic_papers = (
        academic_papers_for_project(db, project.id)
        if flags.get("enable_academic_research")
        else []
    )
    return {
        "metrics": data,
        "corpus": corpus,
        "official_facts": official_facts,
        "flags": flags,
        "fact_events": fact_events,
        "operation_events": operation_events,
        "fact_evidence": fact_evidence,
        "academic_papers": academic_papers,
    }


def _writer_instructions(flags: dict) -> str:
    instructions = (
        "Estruture o texto nas seções fornecidas pelo schema. "
        + "A métrica isp_mentioned_items/isp_mention_percent mede somente MENÇÃO textual à instituição. "
        + "Nunca a descreva como protagonismo, liderança, destaque institucional ou centralidade editorial sem evidência específica. "
    )
    if flags["enable_fact_layer"]:
        instructions += (
            "A tabela factual será renderizada diretamente pelo sistema; "
            "fact_layer_intro deve apenas contextualizá-la, sem reescrever ou alterar os fatos."
        )
    else:
        instructions += (
            "Este perfil NÃO usa camada factual individual. Não introduza nomes de vítimas, listas nominais "
            "ou reconstruções de ocorrências individuais apenas para preencher essa seção. "
            "Use fact_layer_intro como uma frase curta indicando que a camada factual individual não integrou o escopo."
        )
    return instructions


def _agent_payload(db: Session, project: Project, grounding: dict) -> dict:
    return {
        "project": project_payload(project, for_report=True),
        "metrics": grounding["metrics"],
        "official_facts": [
            {
                "label": fact.label,
                "value": fact.value,
                "evidence": fact.evidence,
                "source": fact.source_reference,
                "indicator": fact.indicator,
                "geography": fact.geography,
                "period_start": fact.period_start.isoformat() if fact.period_start else None,
                "period_end": fact.period_end.isoformat() if fact.period_end else None,
                "unit": fact.unit,
            }
            for fact in grounding["official_facts"]
        ],
        "fact_events": grounding["fact_events"],
        "operation_events": grounding["operation_events"],
        "academic_context": grounding["academic_papers"],
        "validated_items": [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "published_at": item.get("published_at"),
                "evidence": item.get("evidence"),
                "theme": item.get("theme"),
                "framing": item.get("framing"),
                "duplicate_count": item.get("duplicate_count", 0),
            }
            for item in grounding["corpus"]
        ],
    }


def _save_report(db: Session, project: Project, result: dict, grounding: dict) -> dict:
    corpus = corpus_for_project(db, project.id)
    traditional, social = split_corpus(corpus)
    payload = {
        "report": result,
        "metrics": grounding["metrics"],
        "corpus": corpus,
        "traditional_corpus": traditional,
        "social_corpus": social,
        "corpus_by_origin": split_corpus_by_origin(corpus),
        "fact_events": grounding["fact_events"],
        "fact_evidence": grounding["fact_evidence"],
        "academic_papers": grounding["academic_papers"],
        "word_cloud": word_cloud_for_project(db, project.id),
        "project": project_payload(project, for_report=True),
    }
    digest = content_hash(payload)
    fingerprint = request_fingerprint(project)

    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if saved and saved.current_version_id and not saved.request_fingerprint:
        # Legado sem fingerprint: passa a constar sem invalidar a versão atual.
        saved.request_fingerprint = fingerprint
        db.commit()
    if saved and saved.current_version_id:
        current = db.get(ReportVersion, saved.current_version_id)
        if current and current.content_hash == digest:
            # Mesma entrada/grounding => nenhuma versão estrutural nova.
            return payload

    previous_no = saved.version_no if saved and saved.version_no else 0
    version = ReportVersion(
        project_id=project.id,
        version_no=previous_no + 1,
        content_hash=digest,
        body=payload,
        qa_status="PENDING",
        qa_findings=[],
    )
    db.add(version)
    db.flush()
    if saved:
        saved.body = payload
        saved.qa_status = "PENDING"
        saved.qa_findings = []
        saved.current_version_id = version.id
        saved.version_no = version.version_no
        saved.content_hash = digest
        saved.request_fingerprint = fingerprint
    else:
        db.add(
            GeneratedReport(
                project_id=project.id,
                body=payload,
                qa_status="PENDING",
                qa_findings=[],
                current_version_id=version.id,
                version_no=1,
                content_hash=digest,
                request_fingerprint=fingerprint,
            )
        )
    db.commit()
    return payload


def draft_report_with_llm(db: Session, project: Project) -> dict:
    if not llm_is_configured():
        raise RuntimeError("Uma chave de LLM é necessária para redigir o relatório")

    grounding = _writer_grounding(db, project)
    result = get_report_agent().run(
        task="report_writer",
        extra_instructions=_writer_instructions(grounding["flags"]),
        payload=_agent_payload(db, project, grounding),
        schema_name="structured_media_report_v2",
        response_model=StructuredMediaReportResponse,
        max_output_tokens=7000,
    )
    return _save_report(db, project, result, grounding)


def revise_report_with_llm(
    db: Session,
    project: Project,
    previous_report: dict,
    blocking_findings: list[dict],
) -> dict:
    """Reescreve o rascunho guiado pelos achados bloqueadores do QA."""
    if not llm_is_configured():
        raise RuntimeError("Uma chave de LLM é necessária para revisar o relatório")

    grounding = _writer_grounding(db, project)
    result = get_report_agent().run(
        task="report_reviser",
        extra_instructions=_writer_instructions(grounding["flags"]),
        payload={
            **_agent_payload(db, project, grounding),
            "previous_report": previous_report,
            "blocking_findings": [
                {
                    "severity": finding.get("severity"),
                    "code": finding.get("code"),
                    "message": finding.get("message"),
                }
                for finding in blocking_findings
            ],
        },
        schema_name="structured_media_report_v2",
        response_model=StructuredMediaReportResponse,
        max_output_tokens=7000,
    )
    return _save_report(db, project, result, grounding)


def refine_report_with_qa(
    db: Session,
    project: Project,
    drafted: dict,
    qa: dict,
    *,
    progress_detail=None,
) -> tuple[dict, dict, int]:
    """Loop QA -> revisão: cada rodada custa 1 redação + 1 QA (teto configurável).

    Retorna ``(drafted, qa, refinements)`` com o último estado persistido.
    """
    settings = get_settings()
    max_refinements = max(0, int(settings.max_qa_refinements))
    refinements = 0
    while not qa.get("approved") and refinements < max_refinements:
        blocking = [
            finding for finding in qa.get("findings", [])
            if finding.get("severity") in {"CRITICAL", "HIGH"}
        ]
        if not blocking:
            break
        refinements += 1
        if progress_detail:
            progress_detail(
                f"Revisão {refinements}/{max_refinements}: "
                f"{len(blocking)} achado(s) bloqueador(es) do QA"
            )
        try:
            drafted = revise_report_with_llm(db, project, drafted["report"], blocking)
        except RuntimeError:
            refinements -= 1
            break
        qa = run_report_qa(db, project, drafted)
    return drafted, qa, refinements


def export_report_pdf(db: Session, project: Project, *, allow_draft: bool = False) -> bytes:
    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if not saved:
        raise RuntimeError("Relatório ainda não foi gerado")

    # Relatórios antigos, relatórios gerados por /ai/report ou execuções que
    # persistiram o texto antes do QA podem permanecer com status PENDING.
    # Ao solicitar o PDF final, executamos o QA uma vez antes de bloquear a
    # exportação. Isso evita exigir uma chamada manual ao endpoint /qa.
    if not allow_draft and saved.qa_status == "PENDING":
        qa_payload = hydrate_cached_report(db, project, saved)
        run_report_qa(db, project, qa_payload)
        db.refresh(saved)

    if not allow_draft and saved.qa_status != "APPROVED":
        blocking = [
            finding
            for finding in (saved.qa_findings or [])
            if finding.get("severity") in {"CRITICAL", "HIGH"}
        ]
        detail = "; ".join(
            f"{finding.get('code', 'QA')}: {finding.get('message', '')}"
            for finding in blocking[:3]
        )
        suffix = f" Motivo(s): {detail}" if detail else ""
        raise RuntimeError(
            f"O relatório não passou pelo QA final (status: {saved.qa_status})."
            f"{suffix} Use export-draft.pdf apenas para revisão interna."
        )

    payload = hydrate_cached_report(db, project, saved)
    return build_pdf(payload)



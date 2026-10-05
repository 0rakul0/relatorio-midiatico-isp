from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.billing import plan_allows
from app.models import Project
from app.orchestration.state import RunCancelled
from app.services.classification import classify_with_llm
from app.services.collection.web import collect_web
from app.services.news_validation import validate_news_stage
from app.services.search_planning import detect_coverage_gaps, plan_gap_fill_queries


StageCallback = Callable[[str, str, str | None], None]


def run_gap_fill_stage(
    db: Session,
    project: Project,
    *,
    flags: dict,
    stage: StageCallback,
    check: Callable[[], None],
    detail_for: Callable[[str], Callable[[str], None]],
) -> dict:
    gap_fill: dict = {
        "created": 0,
        "collected": 0,
        "validated": 0,
        "status": "SKIPPED",
        "reason": "",
        "zero_corpus_recovery": False,
    }
    check()
    gaps = detect_coverage_gaps(db, project)
    zero_corpus = bool(gaps.get("zero_corpus"))
    gap_fill["zero_corpus_recovery"] = zero_corpus
    gap_state = (project.execution_plan or {}).get("gap_fill") or {}
    zero_recovery_already_done = bool(gap_state.get("zero_corpus_recovery"))

    if gap_state.get("completed") and (not zero_corpus or zero_recovery_already_done):
        gap_fill["reason"] = "Cobertura complementar já executada neste projeto"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
        return gap_fill

    if not flags.get("enable_web_collection", True):
        gap_fill["reason"] = "Coleta web desativada pelo plano"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
        return gap_fill

    if not zero_corpus and not plan_allows(db, project, "gap_fill"):
        gap_fill["reason"] = "Plano atual não inclui cobertura complementar"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
        return gap_fill

    if not gaps["needs_fill"]:
        gap_fill["reason"] = "Sem lacunas acionáveis após a validação"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
        return gap_fill

    portals = ", ".join(entry["portal"] for entry in gaps["uncovered_portals"])
    detail = (
        "Corpus jornalístico zero: executando recuperação obrigatória "
        "com grafias alternativas, vocabulário jornalístico e consulta mais ampla"
        if zero_corpus
        else f"Lacunas em: {portals[:180]}"
    )
    stage("gap_fill", "RUNNING", detail)

    created = plan_gap_fill_queries(db, project, gaps)
    gap_fill["created"] = len(created)
    if not created:
        gap_fill["reason"] = (
            "Corpus zero, mas nenhuma consulta de recuperação válida pôde ser criada"
            if zero_corpus
            else "Nenhuma consulta complementar válida para as lacunas"
        )
        stage("gap_fill", "DONE", gap_fill["reason"])
        return gap_fill

    completed_ok = False
    try:
        gap_fill["collected"] = collect_web(
            db,
            project.id,
            cancel_check=check,
            progress_detail=detail_for("gap_fill"),
        )
        gap_validation = validate_news_stage(
            db,
            project,
            cancel_check=check,
            progress_detail=detail_for("gap_fill"),
        )
        gap_fill["validated"] = int(gap_validation.get("valid", 0))
        classify_with_llm(
            db,
            project,
            cancel_check=check,
            progress_detail=detail_for("gap_fill"),
        )
        completed_ok = True
        stage(
            "gap_fill",
            "DONE",
            (
                f"Recuperação de corpus zero: {len(created)} consulta(s); "
                if zero_corpus
                else f"{len(created)} consulta(s) complementar(es); "
            )
            + f"{gap_fill['collected']} URL(s) nova(s); "
            + f"{gap_fill['validated']} validada(s)",
        )
    except RuntimeError as exc:
        if isinstance(exc, RunCancelled):
            raise
        gap_fill["reason"] = (
            f"Coleta complementar indisponível: {str(exc)[:150]}"
        )
        stage("gap_fill", "DONE", gap_fill["reason"])

    plan_state = dict(project.execution_plan or {})
    plan_state["gap_fill"] = {
        "completed": completed_ok,
        "created": gap_fill["created"],
        "zero_corpus_recovery": zero_corpus,
        "collected": gap_fill["collected"],
        "validated": gap_fill["validated"],
        "reason": gap_fill["reason"],
    }
    project.execution_plan = plan_state
    db.commit()
    if completed_ok:
        gap_fill["status"] = "DONE"
    return gap_fill

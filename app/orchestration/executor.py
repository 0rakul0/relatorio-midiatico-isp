"""Executor das execuções assíncronas de relatório.

Este é o dono do ciclo de vida do run: dispara ``run_full_methodology`` numa
thread, propaga progresso para ``orchestration.state`` e traduz o resultado
(sucesso, cancelamento, falha) para o estado persistido do projeto. A API
apenas chama ``start_run``; ela não conhece threads nem rollback.
"""

from __future__ import annotations

import logging
from threading import Thread

from app.cost_tracker import cost_context, set_cost_operation
from app.database import SessionLocal
from app.models import GeneratedReport, Project, ReportRun
from app.orchestration.state import (
    RunCancelled,
    check_cancelled,
    create_run,
    create_run_if_none,
    mark_run_cancelled,
    mark_run_completed,
    mark_run_failed,
    mark_run_started,
    run_snapshot,
    update_stage,
)
from app.services.pipeline import run_full_methodology
from app.services.cache import hydrate_cached_report
from app.services.reporting import draft_report_with_llm, refine_report_with_qa
from app.report_qa import run_report_qa
from app.services.qa_refinement import refine_historical_report_until_qa

logger = logging.getLogger(__name__)


def _run_project_worker(run_id: str, project_id: int) -> None:
    mark_run_started(run_id)
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if not project:
            raise RuntimeError("Projeto não encontrado")

        def progress(key: str, status: str, detail: str | None = None) -> None:
            if status == "RUNNING":
                set_cost_operation(key)
            update_stage(run_id, key, status, detail)

        with cost_context(project_id=project_id, run_id=run_id):
            result = run_full_methodology(
                db,
                project,
                progress_callback=progress,
                cancel_check=lambda: check_cancelled(run_id),
            )
        check_cancelled(run_id)
        qa_status = (result.get("qa") or {}).get("status", "N/D")
        mark_run_completed(run_id, f"Relatório concluído. QA: {qa_status}")
    except RunCancelled:
        db.rollback()
        project = db.get(Project, project_id)
        if project:
            project.status = "RUN_CANCELLED"
            db.commit()
        mark_run_cancelled(run_id)
    except Exception as exc:
        logger.exception("Execução do projeto %s falhou (run %s)", project_id, run_id)
        db.rollback()
        project = db.get(Project, project_id)
        if project:
            project.status = "RUN_FAILED"
            db.commit()
        mark_run_failed(run_id, str(exc))
    finally:
        db.close()


def start_run(project_id: int) -> dict:
    """Inicia uma execução assíncrona e devolve o snapshot do run.

    A checagem de execução ativa e a criação do run são atômicas. Se já houver
    execução ativa (neste processo ou persistida por outro worker), devolve o
    snapshot dela sem iniciar outra.
    """
    state, created = create_run_if_none(project_id)
    if not created:
        return run_snapshot(state.run_id) or {}

    thread = Thread(
        target=_run_project_worker,
        args=(state.run_id, project_id),
        name=f"report-run-{state.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return run_snapshot(state.run_id) or {}


def _resume_report_worker(run_id: str, project_id: int) -> None:
    """Retoma somente redação/QA usando o grounding já persistido."""
    mark_run_started(run_id)
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if not project:
            raise RuntimeError("Projeto não encontrado")

        def progress(key: str, status: str, detail: str | None = None) -> None:
            if status == "RUNNING":
                set_cost_operation(key)
            update_stage(run_id, key, status, detail)

        with cost_context(project_id=project_id, run_id=run_id):
            saved = db.query(GeneratedReport).filter(GeneratedReport.project_id == project_id).first()
            if saved and saved.body:
                progress("report", "DONE", "Redação recuperada da versão persistida antes da interrupção")
                drafted = hydrate_cached_report(db, project, saved)
            else:
                progress("report", "RUNNING", "Retomando pela redação; coleta e análise anteriores serão preservadas")
                drafted = draft_report_with_llm(db, project)
                progress("report", "DONE", "Relatório estruturado e persistido")

            check_cancelled(run_id)
            progress("qa", "RUNNING", "Reexecutando QA final e revisão dos achados bloqueadores")
            qa = run_report_qa(db, project, drafted)
            refinements = 0
            if not qa["approved"]:
                drafted, qa, refinements = refine_report_with_qa(
                    db, project, drafted, qa,
                    progress_detail=lambda detail: progress("qa", "RUNNING", detail),
                )
            if refinements:
                progress("report", "DONE", f"Relatório revisado {refinements}x a partir dos achados do QA")
            progress("qa", "DONE", f"QA {qa['status']}" + (f" após {refinements} revisão(ões)" if refinements else ""))
            project.status = "REPORT_READY" if qa["approved"] else "REPORT_NEEDS_REVIEW"
            db.commit()
        mark_run_completed(run_id, f"Relatório retomado. QA: {qa['status']}")
    except RunCancelled:
        db.rollback()
        project = db.get(Project, project_id)
        if project:
            project.status = "RUN_CANCELLED"
            db.commit()
        mark_run_cancelled(run_id)
    except Exception as exc:
        logger.exception("Retomada do projeto %s falhou (run %s)", project_id, run_id)
        db.rollback()
        project = db.get(Project, project_id)
        if project:
            project.status = "RUN_FAILED"
            db.commit()
        mark_run_failed(run_id, str(exc))
    finally:
        db.close()


def resume_run(project_id: int) -> dict:
    """Cria um novo run que reaproveita todas as etapas anteriores à redação."""
    # Um processo morto pode ter deixado ReportRun como RUNNING. Encerramos
    # esse registro antes de criar a retomada, pois não há thread após reboot.
    db = SessionLocal()
    try:
        stale = db.query(ReportRun).filter(
            ReportRun.project_id == project_id,
            ReportRun.status.in_(("PENDING", "RUNNING")),
        ).all()
        for row in stale:
            row.status = "FAILED"
            row.message = "Execução anterior interrompida; substituída por retomada"
        db.commit()
    finally:
        db.close()

    state = create_run(project_id)
    # As etapas 1..9 já produziram dados persistentes. Na nova execução elas
    # aparecem como recuperadas, sem repetir busca, validação ou classificação.
    for key, _label in RUN_STAGES:
        if key in {"report", "qa"}:
            break
        update_stage(state.run_id, key, "SKIPPED", "Reaproveitado da execução anterior")
    thread = Thread(
        target=_resume_report_worker,
        args=(state.run_id, project_id),
        name=f"report-resume-{state.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return run_snapshot(state.run_id) or {}



def _refine_qa_worker(run_id: str, project_id: int) -> None:
    mark_run_started(run_id)
    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if not project:
            raise RuntimeError("Projeto não encontrado")

        def progress(key: str, status: str, detail: str | None = None) -> None:
            if status == "RUNNING":
                set_cost_operation(f"qa_refinement:{key}")
            update_stage(run_id, key, status, detail)

        with cost_context(project_id=project_id, run_id=run_id):
            result = refine_historical_report_until_qa(
                db,
                project,
                progress=progress,
                cancel_check=lambda: check_cancelled(run_id),
            )
        qa_status = (result.get("qa") or {}).get("status", "N/D")
        mark_run_completed(run_id, f"Refinamento concluído. QA: {qa_status}")
    except RunCancelled:
        db.rollback()
        mark_run_cancelled(run_id)
    except Exception as exc:
        logger.exception("Refinamento QA do projeto %s falhou (run %s)", project_id, run_id)
        db.rollback()
        mark_run_failed(run_id, str(exc))
    finally:
        db.close()


def start_qa_refinement(project_id: int) -> dict:
    state = create_run(project_id)
    active = {
        "validation",
        "facts_pass_2",
        "fact_resolution_2",
        "classification",
        "gap_fill",
        "report",
        "qa",
    }
    for key, _label in RUN_STAGES:
        if key not in active:
            update_stage(
                state.run_id,
                key,
                "SKIPPED",
                "Preservado do relatório histórico",
            )
    thread = Thread(
        target=_refine_qa_worker,
        args=(state.run_id, project_id),
        name=f"qa-refine-{state.run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return run_snapshot(state.run_id) or {}

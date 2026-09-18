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
from app.models import Project
from app.orchestration.state import (
    RunCancelled,
    check_cancelled,
    create_run_if_none,
    mark_run_cancelled,
    mark_run_completed,
    mark_run_failed,
    mark_run_started,
    run_snapshot,
    update_stage,
)
from app.services.pipeline import run_full_methodology

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

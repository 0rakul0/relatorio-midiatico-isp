"""Camada de orquestração: estado do run, executor e, futuramente, estágios.

Os atalhos do executor são carregados sob demanda para que módulos de pipeline
possam importar ``orchestration.state`` sem criar um ciclo de importação.
"""
from app.orchestration.state import (
    RUN_STAGES,
    RunCancelled,
    RunStageState,
    RunState,
    active_run_for_project,
    check_cancelled,
    create_run,
    create_run_if_none,
    get_run,
    mark_run_cancelled,
    mark_run_completed,
    mark_run_failed,
    mark_run_started,
    request_cancel,
    run_snapshot,
    update_stage,
)


def start_run(*args, **kwargs):
    """Inicia uma execução sem importar o executor durante a carga do pacote."""
    from app.orchestration.executor import start_run as _start_run

    return _start_run(*args, **kwargs)


def resume_run(*args, **kwargs):
    """Retoma uma execução com carregamento tardio do executor."""
    from app.orchestration.executor import resume_run as _resume_run

    return _resume_run(*args, **kwargs)


def start_qa_refinement(*args, **kwargs):
    """Inicia refinamento de QA com carregamento tardio do executor."""
    from app.orchestration.executor import start_qa_refinement as _start_qa_refinement

    return _start_qa_refinement(*args, **kwargs)

__all__ = [
    "RUN_STAGES",
    "RunCancelled",
    "RunStageState",
    "RunState",
    "active_run_for_project",
    "check_cancelled",
    "create_run",
    "create_run_if_none",
    "get_run",
    "mark_run_cancelled",
    "mark_run_completed",
    "mark_run_failed",
    "mark_run_started",
    "request_cancel",
    "run_snapshot",
    "start_run",
    "resume_run",
    "start_qa_refinement",
    "update_stage",
]

"""Registro de estado das execuções (progresso, estágios e cancelamento).

Este módulo não executa nada: apenas mantém em memória o ``RunState`` de cada
run, expõe os estágios declarados em ``RUN_STAGES`` e implementa o cancelamento
cooperativo. Quem efetivamente dispara a pipeline é ``orchestration.executor``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock
from uuid import uuid4


RUN_STAGES = [
    ("profile", "Perfil do tema"),
    ("search_plan", "Planejamento de buscas"),
    ("collection", "Coleta em sites"),
    ("youtube", "Coleta no YouTube"),
    ("cross_validation", "Validação cruzada Tavily × DuckDuckGo Videos"),
    ("facts_pass_1", "Extração factual - 1ª passagem"),
    ("fact_resolution_1", "Consolidação factual - 1ª passagem"),
    ("nominal_plan", "Planejamento de buscas nominais"),
    ("nominal_collection", "Coleta nominal"),
    ("facts_pass_2", "Extração factual - 2ª passagem"),
    ("fact_resolution_2", "Consolidação factual - 2ª passagem"),
    ("validation", "Validação do corpus"),
    ("classification", "Análise e classificação"),
    ("report", "Redação do relatório"),
    ("qa", "Auditoria QA final"),
]


class RunCancelled(RuntimeError):
    """Sinaliza cancelamento cooperativo solicitado pelo usuário."""


@dataclass
class RunStageState:
    key: str
    label: str
    status: str = "PENDING"
    detail: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class RunState:
    run_id: str
    project_id: int
    status: str = "PENDING"
    message: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    stages: dict[str, RunStageState] = field(default_factory=dict)


_lock = Lock()
_runs: dict[str, RunState] = {}
_cancel_events: dict[str, Event] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(project_id: int) -> RunState:
    run_id = uuid4().hex
    state = RunState(
        run_id=run_id,
        project_id=project_id,
        stages={key: RunStageState(key=key, label=label) for key, label in RUN_STAGES},
    )
    with _lock:
        _runs[run_id] = state
        _cancel_events[run_id] = Event()
    return state


def get_run(run_id: str) -> RunState | None:
    with _lock:
        return _runs.get(run_id)


def active_run_for_project(project_id: int) -> dict | None:
    with _lock:
        for state in _runs.values():
            if state.project_id == project_id and state.status in {"PENDING", "RUNNING"}:
                return {
                    "run_id": state.run_id,
                    "project_id": state.project_id,
                    "status": state.status,
                    "message": state.message,
                    "cancel_requested": state.cancel_requested,
                    "started_at": state.started_at,
                    "finished_at": state.finished_at,
                    "stages": [
                        {
                            "key": stage.key,
                            "label": stage.label,
                            "status": stage.status,
                            "detail": stage.detail,
                            "started_at": stage.started_at,
                            "finished_at": stage.finished_at,
                        }
                        for stage in state.stages.values()
                    ],
                }
    return None


def run_snapshot(run_id: str) -> dict | None:
    with _lock:
        state = _runs.get(run_id)
        if not state:
            return None
        return {
            "run_id": state.run_id,
            "project_id": state.project_id,
            "status": state.status,
            "message": state.message,
            "error": state.error,
            "cancel_requested": state.cancel_requested,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "stages": [
                {
                    "key": stage.key,
                    "label": stage.label,
                    "status": stage.status,
                    "detail": stage.detail,
                    "started_at": stage.started_at,
                    "finished_at": stage.finished_at,
                }
                for stage in state.stages.values()
            ],
        }


def mark_run_started(run_id: str) -> None:
    with _lock:
        state = _runs[run_id]
        state.status = "RUNNING"
        state.started_at = state.started_at or _now()
        state.message = "Execução iniciada"


def mark_run_completed(run_id: str, message: str = "Relatório concluído") -> None:
    with _lock:
        state = _runs[run_id]
        state.status = "COMPLETED"
        state.finished_at = _now()
        state.message = message


def mark_run_cancelled(run_id: str, message: str = "Execução interrompida pelo usuário") -> None:
    with _lock:
        state = _runs[run_id]
        state.status = "CANCELLED"
        state.finished_at = _now()
        state.message = message
        # A etapa que estava rodando fica explicitamente interrompida.
        for stage in state.stages.values():
            if stage.status == "RUNNING":
                stage.status = "CANCELLED"
                stage.finished_at = _now()
                stage.detail = message


def mark_run_failed(run_id: str, error: str) -> None:
    with _lock:
        state = _runs[run_id]
        state.status = "FAILED"
        state.error = error
        state.message = "Execução encerrada com erro"
        state.finished_at = _now()
        for stage in state.stages.values():
            if stage.status == "RUNNING":
                stage.status = "FAILED"
                stage.finished_at = _now()
                stage.detail = error


def update_stage(run_id: str, key: str, status: str, detail: str | None = None) -> None:
    with _lock:
        state = _runs[run_id]
        stage = state.stages.get(key)
        if not stage:
            return
        previous = stage.status
        stage.status = status
        if detail is not None:
            stage.detail = detail
        if status == "RUNNING" and previous != "RUNNING":
            stage.started_at = _now()
            stage.finished_at = None
        if status in {"DONE", "FAILED", "CANCELLED", "SKIPPED"}:
            stage.finished_at = _now()


def request_cancel(run_id: str) -> bool:
    with _lock:
        state = _runs.get(run_id)
        event = _cancel_events.get(run_id)
        if not state or not event:
            return False
        if state.status in {"COMPLETED", "CANCELLED", "FAILED"}:
            return False
        state.cancel_requested = True
        state.message = "Cancelamento solicitado; aguardando um ponto seguro para interromper"
        event.set()
        return True


def check_cancelled(run_id: str) -> None:
    with _lock:
        event = _cancel_events.get(run_id)
    if event and event.is_set():
        raise RunCancelled("Execução interrompida pelo usuário")

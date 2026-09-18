"""Registro de estado das execuções (progresso, estágios e cancelamento).

O estado é mantido em memória para leitura rápida e persistido em
``report_runs`` para sobreviver a reinícios e permitir coordenação entre
múltiplos workers. Este módulo não executa a pipeline; quem a dispara é
``orchestration.executor``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock
from uuid import uuid4

from sqlalchemy import select

from app.database import SessionLocal
from app.models import ReportRun


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

_ACTIVE_STATUSES = {"PENDING", "RUNNING"}
_TERMINAL_STATUSES = {"COMPLETED", "CANCELLED", "FAILED"}
_DB_POLL_INTERVAL_SECONDS = 2.0


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
_last_db_poll: dict[str, float] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_stages(state: RunState) -> list[dict]:
    return [
        {
            "key": stage.key,
            "label": stage.label,
            "status": stage.status,
            "detail": stage.detail,
            "started_at": stage.started_at,
            "finished_at": stage.finished_at,
        }
        for stage in state.stages.values()
    ]


def _snapshot(state: RunState) -> dict:
    return {
        "run_id": state.run_id,
        "project_id": state.project_id,
        "status": state.status,
        "message": state.message,
        "error": state.error,
        "cancel_requested": state.cancel_requested,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "stages": _serialize_stages(state),
    }


def _state_from_row(row: ReportRun) -> RunState:
    state = RunState(
        run_id=row.run_id,
        project_id=row.project_id,
        status=row.status or "PENDING",
        message=row.message,
        error=row.error,
        cancel_requested=bool(row.cancel_requested),
        started_at=row.started_at,
        finished_at=row.finished_at,
    )
    stored = {
        item.get("key"): item
        for item in (row.stages or [])
        if isinstance(item, dict) and item.get("key")
    }
    state.stages = {
        key: RunStageState(
            key=key,
            label=(stored.get(key) or {}).get("label") or label,
            status=(stored.get(key) or {}).get("status") or "PENDING",
            detail=(stored.get(key) or {}).get("detail"),
            started_at=(stored.get(key) or {}).get("started_at"),
            finished_at=(stored.get(key) or {}).get("finished_at"),
        )
        for key, label in RUN_STAGES
    }
    return state


def _persist(state: RunState) -> None:
    """Persistência best-effort: nunca deve derrubar a execução."""
    try:
        session = SessionLocal()
    except Exception:
        return
    try:
        row = session.get(ReportRun, state.run_id)
        if row is None:
            row = ReportRun(run_id=state.run_id, project_id=state.project_id)
            session.add(row)
        row.status = state.status
        row.message = state.message
        row.error = state.error
        row.cancel_requested = state.cancel_requested
        row.started_at = state.started_at
        row.finished_at = state.finished_at
        row.stages = _serialize_stages(state)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _load_from_db(run_id: str) -> RunState | None:
    try:
        session = SessionLocal()
    except Exception:
        return None
    try:
        row = session.get(ReportRun, run_id)
        return _state_from_row(row) if row else None
    except Exception:
        return None
    finally:
        session.close()


def _load_active_from_db(project_id: int) -> RunState | None:
    try:
        session = SessionLocal()
    except Exception:
        return None
    try:
        row = session.scalars(
            select(ReportRun)
            .where(
                ReportRun.project_id == project_id,
                ReportRun.status.in_(tuple(_ACTIVE_STATUSES)),
            )
            .order_by(ReportRun.created_at.desc())
        ).first()
        return _state_from_row(row) if row else None
    except Exception:
        return None
    finally:
        session.close()


def _get_or_load(run_id: str) -> RunState | None:
    with _lock:
        state = _runs.get(run_id)
    if state is not None:
        return state
    loaded = _load_from_db(run_id)
    if loaded is None:
        return None
    with _lock:
        _runs.setdefault(run_id, loaded)
        _cancel_events.setdefault(run_id, Event())
    return loaded


def _new_state(project_id: int) -> RunState:
    run_id = uuid4().hex
    return RunState(
        run_id=run_id,
        project_id=project_id,
        stages={key: RunStageState(key=key, label=label) for key, label in RUN_STAGES},
    )


def create_run(project_id: int) -> RunState:
    state = _new_state(project_id)
    with _lock:
        _runs[state.run_id] = state
        _cancel_events[state.run_id] = Event()
    _persist(state)
    return state


def create_run_if_none(project_id: int) -> tuple[RunState, bool]:
    """Cria uma execução apenas se não houver outra ativa para o projeto.

    A checagem e a criação acontecem sob o mesmo lock, eliminando a corrida
    entre ``active_run_for_project`` e ``create_run``. Execuções ativas de
    outros workers são detectadas pelo estado persistido.
    """
    with _lock:
        for state in _runs.values():
            if state.project_id == project_id and state.status in _ACTIVE_STATUSES:
                return state, False

        persisted = _load_active_from_db(project_id)
        if persisted is not None:
            _runs.setdefault(persisted.run_id, persisted)
            _cancel_events.setdefault(persisted.run_id, Event())
            return persisted, False

        state = _new_state(project_id)
        _runs[state.run_id] = state
        _cancel_events[state.run_id] = Event()

    _persist(state)
    return state, True


def get_run(run_id: str) -> RunState | None:
    return _get_or_load(run_id)


def active_run_for_project(project_id: int) -> dict | None:
    with _lock:
        for state in _runs.values():
            if state.project_id == project_id and state.status in _ACTIVE_STATUSES:
                return _snapshot(state)
    persisted = _load_active_from_db(project_id)
    return _snapshot(persisted) if persisted else None


def run_snapshot(run_id: str) -> dict | None:
    state = _get_or_load(run_id)
    return _snapshot(state) if state else None


def mark_run_started(run_id: str) -> None:
    state = _get_or_load(run_id)
    if state is None:
        return
    with _lock:
        state.status = "RUNNING"
        state.started_at = state.started_at or _now()
        state.message = "Execução iniciada"
    _persist(state)


def mark_run_completed(run_id: str, message: str = "Relatório concluído") -> None:
    state = _get_or_load(run_id)
    if state is None:
        return
    with _lock:
        state.status = "COMPLETED"
        state.finished_at = _now()
        state.message = message
    _persist(state)


def mark_run_cancelled(run_id: str, message: str = "Execução interrompida pelo usuário") -> None:
    state = _get_or_load(run_id)
    if state is None:
        return
    with _lock:
        state.status = "CANCELLED"
        state.finished_at = _now()
        state.message = message
        # A etapa que estava rodando fica explicitamente interrompida.
        for stage in state.stages.values():
            if stage.status == "RUNNING":
                stage.status = "CANCELLED"
                stage.finished_at = _now()
                stage.detail = message
    _persist(state)


def mark_run_failed(run_id: str, error: str) -> None:
    state = _get_or_load(run_id)
    if state is None:
        return
    with _lock:
        state.status = "FAILED"
        state.error = error
        state.message = "Execução encerrada com erro"
        state.finished_at = _now()
        for stage in state.stages.values():
            if stage.status == "RUNNING":
                stage.status = "FAILED"
                stage.finished_at = _now()
                stage.detail = error
    _persist(state)


def update_stage(run_id: str, key: str, status: str, detail: str | None = None) -> None:
    state = _get_or_load(run_id)
    if state is None:
        return
    with _lock:
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
    _persist(state)


def request_cancel(run_id: str) -> bool:
    state = _get_or_load(run_id)
    with _lock:
        event = _cancel_events.get(run_id)
        if state is None or event is None:
            return False
        if state.status in _TERMINAL_STATUSES:
            return False
        state.cancel_requested = True
        state.message = "Cancelamento solicitado; aguardando um ponto seguro para interromper"
        event.set()
    _persist(state)
    return True


def check_cancelled(run_id: str) -> None:
    event = _cancel_events.get(run_id)
    if event and event.is_set():
        raise RunCancelled("Execução interrompida pelo usuário")

    state = _runs.get(run_id)
    if state is not None and state.cancel_requested:
        raise RunCancelled("Execução interrompida pelo usuário")

    # Coordenação entre workers: consulta o flag persistido, com throttling
    # para não fazer uma leitura por item processado.
    now = time.monotonic()
    if now - _last_db_poll.get(run_id, 0.0) < _DB_POLL_INTERVAL_SECONDS:
        return
    _last_db_poll[run_id] = now

    persisted = _load_from_db(run_id)
    if persisted is None or not persisted.cancel_requested:
        return

    with _lock:
        existing = _runs.get(run_id)
        if existing is not None:
            existing.cancel_requested = True
        local_event = _cancel_events.setdefault(run_id, Event())
        local_event.set()
    raise RunCancelled("Execução interrompida pelo usuário")

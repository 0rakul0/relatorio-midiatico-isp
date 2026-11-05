"""Provas de concorrencia entre usuarios, fila e liberacao de lock SQLite."""
from __future__ import annotations

import threading
import time
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Project, ReportRun, SearchQuery
from app.orchestration import state as run_state
from app.orchestration import scheduler as run_scheduler
from app.services.collection import orchestrator
from app.services.collection.web import new_web_counters
from app.tools import search as search_tools
from app.config import Settings


def test_different_projects_run_together_and_a_third_waits(monkeypatch):
    monkeypatch.setattr(run_scheduler, "check_cancelled", lambda _run_id: None)
    pool = run_scheduler.RunScheduler(concurrent=2, queued=1)
    release = threading.Event()
    two_started = threading.Event()
    third_started = threading.Event()
    lock = threading.Lock()
    started: list[int] = []

    def worker(run_id, project_id):
        with lock:
            started.append(project_id)
            if len(started) == 2:
                two_started.set()
            if project_id == 3:
                third_started.set()
        assert release.wait(5)

    try:
        pool.reserve()
        pool.submit_reserved(worker, "run-1", 1)
        pool.reserve()
        pool.submit_reserved(worker, "run-2", 2)
        assert two_started.wait(4)
        assert set(started) == {1, 2}
        pool.reserve()
        pool.submit_reserved(worker, "run-3", 3)
        with pytest.raises(run_scheduler.RunQueueFull):
            pool.reserve()
        assert not third_started.wait(0.1)
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert third_started.is_set()
    assert set(started) == {1, 2, 3}


def test_cancelled_queued_run_never_starts(monkeypatch):
    first_started = threading.Event()
    release = threading.Event()
    cancelled = []
    executed = []

    def check(run_id):
        if run_id == "run-waiting":
            raise run_scheduler.RunCancelled("cancelled")

    monkeypatch.setattr(run_scheduler, "check_cancelled", check)
    monkeypatch.setattr(
        run_scheduler, "mark_run_cancelled",
        lambda run_id: cancelled.append(run_id),
    )
    pool = run_scheduler.RunScheduler(concurrent=1, queued=1)

    def worker(run_id, project_id):
        executed.append(run_id)
        if run_id == "run-1":
            first_started.set()
            assert release.wait(5)

    try:
        pool.reserve()
        pool.submit_reserved(worker, "run-1", 1)
        assert first_started.wait(3)
        pool.reserve()
        pool.submit_reserved(worker, "run-waiting", 2)
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert executed == ["run-1"]
    assert cancelled == ["run-waiting"]


def test_per_project_lock_does_not_block_other_project(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(run_state, "SessionLocal", factory)
    monkeypatch.setattr(run_state, "_runs", {})
    monkeypatch.setattr(run_state, "_cancel_events", {})
    try:
        first, created_first = run_state.create_run_if_none(1111)
        second, created_second = run_state.create_run_if_none(2222)
        repeat, created_repeat = run_state.create_run_if_none(1111)
        assert created_first and created_second
        assert not created_repeat
        assert first.run_id != second.run_id
        assert repeat.run_id == first.run_id
        assert second.status == "PENDING"
    finally:
        engine.dispose()


def test_web_search_releases_writer_while_second_query_is_still_in_network(
    monkeypatch, tmp_path
):
    """Segunda conta pode criar projeto durante HTTP de outra conta."""
    dbfile = tmp_path / "multiuser.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{dbfile}",
        connect_args={"timeout": 1.0, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=True)
    with factory() as db:
        project = Project(
            topic="Dossiê Mulher 2026",
            launch_date=date(2026, 1, 1),
            collection_start=date(2026, 1, 1),
            collection_end=date(2026, 12, 31),
            topic_profile={},
        )
        db.add(project)
        db.flush()
        project_id = project.id
        for query in ("busca-1", "busca-2"):
            db.add(SearchQuery(
                project_id=project_id, query=query,
                kind="media_primary", purpose="MEDIA_REPERCUSSION",
                rationale="teste", priority=1,
            ))
        db.commit()

    first_finished = threading.Event()
    release_second = threading.Event()
    second_waiting = threading.Event()
    finished = {}
    errors = []
    original_finished = orchestrator.SearchAuditObserver.query_finished

    def finished_with_signal(self, *, query, tool_name):
        original_finished(self, query=query, tool_name=tool_name)
        if query == "busca-1":
            first_finished.set()

    def provider_search(query, **kwargs):
        if query == "busca-2":
            second_waiting.set()
            assert release_second.wait(5)
        return "duckduckgo", []

    def collect():
        try:
            finished["state"] = orchestrator.run_agent_collection(
                project_id=project_id,
                web_queries=["busca-1", "busca-2"],
                web_counters=new_web_counters(250),
            )
        except Exception as exc:
            errors.append(exc)

    settings = Settings(search_parallel_enabled=True, search_parallel_web_workers=2)
    monkeypatch.setattr(orchestrator, "SessionLocal", factory)
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(orchestrator.SearchAuditObserver, "query_finished", finished_with_signal)
    monkeypatch.setattr(search_tools, "_search_web", provider_search)
    monkeypatch.setattr(search_tools, "get_settings", lambda: settings)
    worker = threading.Thread(target=collect, daemon=True)
    try:
        worker.start()
        assert second_waiting.wait(3)
        assert first_finished.wait(4), "primeira consulta deve liberar a escrita"
        with factory() as db:
            another = Project(
                topic="Outro usuario pesquisando outro tema",
                launch_date=date(2026, 1, 1),
                collection_start=date(2026, 1, 1),
                collection_end=date(2026, 12, 31),
            )
            db.add(another)
            db.commit()  # Sem timeout: a consulta 2 permanece na rede.
            assert another.id != project_id
    finally:
        release_second.set()
        worker.join(timeout=8)
        engine.dispose()
    assert not worker.is_alive()
    assert not errors, errors
    assert finished["state"].unavailable is None
    assert len(finished["state"].attempted) == 2

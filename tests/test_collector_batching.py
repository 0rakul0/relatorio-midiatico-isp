"""Regressao para planos com mais consultas que o limite por chamada da tool.

76 consultas do Dossie Mulher -> lotes 50 + 26, preservando a trilha.
Nenhuma busca real e feita nestes testes.
"""
from datetime import date

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent import ReportAgent
from app.config import Settings
from app.database import Base
from app.models import Project, SearchQuery
from app.schema_groups.search import (
    AgentBulkSearchArgs,
    BULK_SEARCH_MAX_QUERIES,
    CollectorExecutionResponse,
)
from app.services.collection import orchestrator
from app.services.collection.web import new_web_counters
from app.tools import search as search_tools


def _bulk_tool(calls, *, failed_query=None):
    def executar_buscas_web(queries: list[str]) -> dict:
        assert 1 <= len(queries) <= BULK_SEARCH_MAX_QUERIES
        calls.append(list(queries))
        return {
            "results": [
                {
                    "query": q,
                    "provider": "duckduckgo",
                    "status": "ERROR" if q == failed_query else "NO_RESULTS",
                    "returned": 0,
                    "accepted": 0,
                    "hits": [],
                }
                for q in queries
            ]
        }

    return StructuredTool.from_function(
        func=executar_buscas_web,
        name="executar_buscas_web",
        description="Executa consultas ja aprovadas",
        args_schema=AgentBulkSearchArgs,
    )


@pytest.mark.parametrize("total,expected_batches", [
    (1, [1]), (49, [49]), (50, [50]),
    (51, [50, 1]), (76, [50, 26]), (125, [50, 50, 25]),
])
def test_collector_processes_all_queries_in_valid_batches(total, expected_batches):
    calls = []
    queries = [f'"Dossiê Mulher 2026" pesquisa fonte {i}' for i in range(total)]
    result = ReportAgent().run(
        task="collector",
        payload={"web_queries": queries},
        response_model=CollectorExecutionResponse,
        tools=[_bulk_tool(calls)],
    )
    assert result["status"] == "COMPLETED"
    assert [len(batch) for batch in calls] == expected_batches
    assert [q for batch in calls for q in batch] == queries
    assert f"{total}/{total}" in result["detail"]
    assert "lote(s)" in result["detail"]


def test_collector_preserves_per_query_failure_status():
    queries = [f"consulta {i}" for i in range(76)]
    calls = []
    result = ReportAgent().run(
        task="collector", payload={"web_queries": queries},
        response_model=CollectorExecutionResponse,
        tools=[_bulk_tool(calls, failed_query="consulta 68")],
    )
    assert result["status"] == "PARTIAL"
    assert "1 com erro/skip" in result["detail"]
    assert [q for batch in calls for q in batch] == queries


def test_collector_reports_which_batch_failed():
    calls = []

    def broken_tool(queries: list[str]) -> dict:
        calls.append(list(queries))
        if len(calls) == 2:
            raise ValueError("provedor temporariamente indisponivel")
        return {
            "results": [
                {"query": q, "provider": "duckduckgo", "status": "NO_RESULTS"}
                for q in queries
            ]
        }

    tool = StructuredTool.from_function(
        func=broken_tool, name="executar_buscas_web",
        description="Busca simulada", args_schema=AgentBulkSearchArgs,
    )
    with pytest.raises(RuntimeError, match=r"lote 2/2, consultas 51-76 de 76"):
        ReportAgent().run(
            task="collector",
            payload={"web_queries": [f"consulta {i}" for i in range(76)]},
            response_model=CollectorExecutionResponse,
            tools=[tool],
        )
    assert [len(part) for part in calls] == [50, 26]


def test_orchestrator_audits_all_76_searches_without_external_requests(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
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
        queries = [f'"Dossiê Mulher 2026" consulta oficial {i}' for i in range(76)]
        for i, query in enumerate(queries):
            db.add(SearchQuery(
                project_id=project_id,
                query=query,
                kind="official_operation_inventory",
                purpose="OFFICIAL_FACT",
                rationale="teste de inventario anual",
                priority=1,
            ))
        db.commit()

    searched = []
    def fake_search(query, **kwargs):
        searched.append(query)
        return "duckduckgo", []

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(search_tools, "_search_web", fake_search)
    monkeypatch.setattr(search_tools, "get_settings", lambda: Settings(
        search_parallel_enabled=False, search_parallel_web_workers=1,
    ))

    state = orchestrator.run_agent_collection(
        project_id=project_id,
        web_queries=queries,
        web_counters=new_web_counters(250),
    )
    assert state.unavailable is None
    assert len(searched) == len(queries) == 76
    assert searched == queries
    assert len(state.attempted) == 76
    assert not state.errors
    with session_factory() as db:
        rows = db.scalars(
            select(SearchQuery)
            .where(SearchQuery.project_id == project_id)
            .order_by(SearchQuery.id)
        ).all()
        assert len(rows) == 76
        assert all(row.executed_at is not None for row in rows)
        assert all(row.execution_status == "NO_RESULTS" for row in rows)
    engine.dispose()

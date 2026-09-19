from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Project, SearchCall, SearchQuery
from app.services.collection import orchestrator
from app.services.collection.common import canonicalize, parse_provider_date
from app.services.collection.web import new_web_counters
from app.tools import search as tools_search


def _make_project(session) -> Project:
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={"actors": [], "actions": [], "locations": []},
    )
    session.add(project)
    session.commit()
    return project


class _BulkAgent:
    def run(self, *, task, payload, response_model, tools, max_tool_rounds, max_output_tokens):
        for tool in tools:
            if tool.name == "executar_buscas_web":
                queries = payload.get("web_queries", [])
                if queries:
                    tool.invoke({"queries": list(queries)})
        return response_model(status="COMPLETED", detail="ok")


def test_zero_result_query_is_recorded_as_executed(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    session.add(
        SearchQuery(
            project_id=project.id,
            query="consulta sem resultados",
            kind="web",
            rationale="teste",
            priority=1,
        )
    )
    session.commit()

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "get_report_agent", lambda: _BulkAgent())
    monkeypatch.setattr(orchestrator, "get_settings", lambda: Settings())
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(tools_search, "get_settings", lambda: Settings())
    monkeypatch.setattr(tools_search, "duckduckgo_news", lambda *_a, **_k: [])
    monkeypatch.setattr(tools_search, "duckduckgo_text", lambda *_a, **_k: [])

    orchestrator.run_agent_collection(
        project_id=project.id,
        web_queries=["consulta sem resultados"],
        web_counters=new_web_counters(250),
    )

    query = session.scalar(select(SearchQuery))
    assert query.executed_at is not None
    assert query.execution_status == "NO_RESULTS"

    calls = session.scalars(select(SearchCall)).all()
    assert len(calls) >= 1
    assert all(call.success is True for call in calls)
    assert all(call.results_returned == 0 for call in calls)
    assert {call.provider for call in calls} == {"duckduckgo"}


def test_canonicalize_strips_only_tracking_and_keeps_semantics():
    assert canonicalize("https://x.com/a?utm_source=nl&id=7") == canonicalize(
        "https://x.com/a?id=7"
    )
    assert canonicalize("https://x.com/a?id=1") != canonicalize("https://x.com/a?id=2")
    assert canonicalize("https://x.com/a?b=2&a=1") == canonicalize(
        "https://x.com/a?a=1&b=2"
    )


def test_parse_provider_date_handles_common_formats():
    assert parse_provider_date("2026-08-12") == date(2026, 8, 12)
    assert parse_provider_date("2026-08-12T10:30:00Z") == date(2026, 8, 12)
    assert parse_provider_date("Wed, 12 Aug 2026 10:00:00 GMT") == date(2026, 8, 12)
    assert parse_provider_date("12/08/2026") == date(2026, 8, 12)
    assert parse_provider_date(None) is None
    assert parse_provider_date("sem data") is None

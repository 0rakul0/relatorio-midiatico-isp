from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import MediaItem, Project, SearchQuery
from app.services.collection import (
    orchestrator,
    web as collection_web,
    youtube as collection_youtube,
)
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


def _web_row() -> dict:
    return {
        "title": "Tema de teste na imprensa",
        "url": "https://midia.example.com/materia",
        "snippet": "Cobertura sobre o tema de teste",
        "content": None,
        "published_at": "2026-08-10",
        "source_name": "Midia",
        "provider": "duckduckgo",
    }



class _FakeAgent:
    """Agente de teste: executa cada consulta do plano via tool, como o real."""

    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, *, task, payload, response_model, tools, max_tool_rounds, max_output_tokens):
        assert task == "collector"
        for tool in tools:
            if tool.name == "executar_buscas_web":
                queries = payload.get("web_queries", [])
            else:
                continue
            if not queries:
                continue
            self.calls.append((tool.name, tuple(queries)))
            tool.invoke({"queries": list(queries)})
        return response_model(status="COMPLETED", detail="ok")


def _patch_search(monkeypatch, session_factory, fake_agent):
    def fake_search_web(query, *, providers=("duckduckgo",), **_kwargs):
        return "duckduckgo", [_web_row()]

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "get_report_agent", lambda: fake_agent)
    monkeypatch.setattr(
        orchestrator, "get_settings", lambda: Settings()
    )
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(collection_web, "search_providers_available", lambda: True)
    monkeypatch.setattr(tools_search, "_search_web", fake_search_web)


def _pending_web_query(session, project) -> None:
    session.add(
        SearchQuery(
            project_id=project.id,
            query="consulta web",
            kind="web",
            rationale="teste",
            priority=1,
        )
    )
    session.commit()


def test_collect_web_is_executed_by_agent(monkeypatch):
    from app import services

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    _pending_web_query(session, project)

    fake_agent = _FakeAgent()
    _patch_search(monkeypatch, session_factory, fake_agent)

    added = services.collect_web(session, project.id)

    assert added == 1
    assert fake_agent.calls == [("executar_buscas_web", ("consulta web",))]
    stored = session.scalars(select(MediaItem)).all()
    assert [item.url for item in stored] == ["https://midia.example.com/materia"]


def test_collect_media_sources_uses_only_web_discovery(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    _pending_web_query(session, project)

    fake_agent = _FakeAgent()
    _patch_search(monkeypatch, session_factory, fake_agent)

    result = collection_youtube.collect_media_sources(session, project)

    assert fake_agent.calls == [("executar_buscas_web", ("consulta web",))]
    assert result["web"]["status"] == "COMPLETED"
    assert result["youtube"]["status"] == "ROUTED"
    assert result["youtube"]["collected"] == 0
    urls = {item.url for item in session.scalars(select(MediaItem)).all()}
    assert urls == {"https://midia.example.com/materia"}


def test_collect_media_sources_marks_unavailable_when_agent_fails(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    _pending_web_query(session, project)

    class _BoomAgent:
        def run(self, **_kwargs):
            raise RuntimeError("agente fora do ar")

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "get_report_agent", lambda: _BoomAgent())
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)

    result = collection_youtube.collect_media_sources(session, project)

    assert result["web"]["status"] == "UNAVAILABLE"
    assert "agente fora do ar" in result["web"]["error"]
    assert result["youtube"]["status"] == "ROUTED"
    assert result["youtube"]["collected"] == 0


def test_collector_executes_required_web_tool_without_llm_decision(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    _pending_web_query(session, project)

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(collection_web, "search_providers_available", lambda: True)
    monkeypatch.setattr(
        tools_search,
        "_search_web",
        lambda query, **_kwargs: ("duckduckgo", [_web_row()]),
    )

    from app import agent as agent_module

    def fail_if_llm_is_called(**_kwargs):
        raise AssertionError("collector nao deve depender de decisao da LLM")

    monkeypatch.setattr(agent_module, "create_chat_model", fail_if_llm_is_called)

    added = collection_web.collect_web(session, project.id)

    assert added == 1
    query = session.scalar(select(SearchQuery).where(SearchQuery.project_id == project.id))
    assert query is not None
    assert query.executed_at is not None
    assert query.execution_status in {"SUCCEEDED", "SUCCEEDED_FLAGGED"}

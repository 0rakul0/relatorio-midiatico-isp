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
from app.services.collection.youtube_helpers import youtube_tasks_for_execution
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


def _video_row() -> dict:
    return {
        "title": "Video sobre tema de teste",
        "url": "https://www.youtube.com/watch?v=abc123",
        "snippet": "Cobertura em video sobre o tema de teste",
        "content": "Cobertura em video sobre o tema de teste",
        "published_at": "2026-08-12",
        "source_name": "ISP RJ",
        "view_count": 100,
        "provider": "duckduckgo_video",
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
            elif tool.name == "executar_buscas_videos":
                queries = payload.get("youtube_queries", [])
            else:
                continue
            if not queries:
                continue
            self.calls.append((tool.name, tuple(queries)))
            tool.invoke({"queries": list(queries)})
        return response_model(status="COMPLETED", detail="ok")


def _patch_search(monkeypatch, session_factory, fake_agent):
    def fake_search_web(query, *, providers=("duckduckgo", "tavily"), **_kwargs):
        return "duckduckgo", [_web_row()]

    def fake_search_videos(query, *, providers=("duckduckgo", "tavily"), **_kwargs):
        if "duckduckgo" in providers:
            return "duckduckgo", [_video_row()]
        return "none", []

    monkeypatch.setattr(orchestrator, "SessionLocal", session_factory)
    monkeypatch.setattr(orchestrator, "get_report_agent", lambda: fake_agent)
    monkeypatch.setattr(
        orchestrator, "get_settings", lambda: Settings(tavily_api_key=None)
    )
    monkeypatch.setattr(orchestrator, "search_providers_available", lambda: True)
    monkeypatch.setattr(collection_web, "search_providers_available", lambda: True)
    monkeypatch.setattr(tools_search, "_search_web", fake_search_web)
    monkeypatch.setattr(tools_search, "_search_videos", fake_search_videos)


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


def test_collect_media_sources_runs_web_and_video_tools(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    project = _make_project(session)
    _pending_web_query(session, project)

    video_queries = [task.query for task in youtube_tasks_for_execution(project)]
    assert video_queries, "o perfil deve gerar pelo menos uma busca de vídeo"

    fake_agent = _FakeAgent()
    _patch_search(monkeypatch, session_factory, fake_agent)

    result = collection_youtube.collect_media_sources(session, project)

    tool_names = {name for name, _query in fake_agent.calls}
    assert tool_names == {"executar_buscas_web", "executar_buscas_videos"}
    web_calls = [query for name, query in fake_agent.calls if name == "executar_buscas_web"]
    video_calls = [query for name, query in fake_agent.calls if name == "executar_buscas_videos"]
    assert web_calls == [("consulta web",)]
    assert video_calls == [tuple(video_queries)]

    assert result["web"]["status"] == "COMPLETED"
    assert result["youtube"]["collected"] == 1
    assert result["youtube"]["status"] == "PARTIAL"
    urls = {item.url for item in session.scalars(select(MediaItem)).all()}
    assert "https://midia.example.com/materia" in urls
    assert "https://www.youtube.com/watch?v=abc123" in urls


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
    assert "agente fora do ar" in result["youtube"]["error"]

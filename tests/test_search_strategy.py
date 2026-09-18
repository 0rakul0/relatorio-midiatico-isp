from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Project, SearchQuery
from app.services import search_planning
from app.services.collection import orchestrator
from app.tools import search as tools_search
from app.topic_profile import heuristic_topic_profile


def _event_project(session) -> Project:
    topic = "policiais mortos em agosto de 2026 no Rio de Janeiro"
    profile = heuristic_topic_profile(topic)
    project = Project(
        topic=topic,
        project_type="EVENT_TOPIC",
        topic_profile=profile,
        launch_date=date(2026, 8, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        event_start=date(2026, 8, 1),
        event_end=date(2026, 8, 31),
    )
    session.add(project)
    session.commit()
    return project


def test_deterministic_plan_is_compact(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = _event_project(session)

    settings = Settings(
        max_complementary_queries=2,
        max_media_queries=12,
        max_fact_queries=1,
        max_official_queries=3,
    )
    monkeypatch.setattr(search_planning, "get_settings", lambda: settings)

    created = search_planning.plan_queries(session, project)
    media = [row for row in created if row.purpose == "MEDIA_REPERCUSSION"]
    facts = [row for row in created if row.purpose == "FACT_DISCOVERY"]
    official = [row for row in created if row.purpose == "OFFICIAL_FACT"]

    assert len(media) <= 12
    assert len([row for row in media if row.kind == "media_primary"]) == 1
    assert len([row for row in media if row.kind == "media_complementary"]) <= 2
    assert len(facts) <= 1
    assert len(official) <= 3


def test_semantic_redundancy_rejects_word_order_only():
    selected = ['"policiais mortos" "Rio de Janeiro" 2026']
    assert search_planning._is_redundant(
        '2026 "Rio de Janeiro" "policiais mortos"',
        selected,
    )


def test_pending_queries_use_independent_purpose_budgets(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    project = _event_project(session)

    for idx in range(20):
        session.add(
            SearchQuery(
                project_id=project.id,
                query=f"media query {idx}",
                kind="media_complementary",
                purpose="MEDIA_REPERCUSSION",
                rationale="test",
                priority=3,
            )
        )
    for idx in range(5):
        session.add(
            SearchQuery(
                project_id=project.id,
                query=f"fact query {idx}",
                kind="fact_discovery",
                purpose="FACT_DISCOVERY",
                rationale="test",
                priority=1,
            )
        )
    session.commit()

    settings = Settings(max_media_queries=4, max_fact_queries=1, max_official_queries=0)
    monkeypatch.setattr(orchestrator, "SessionLocal", factory)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)

    pending = orchestrator.web_queries_pending(project.id)
    assert len([q for q in pending if q.startswith("media")]) == 4
    assert len([q for q in pending if q.startswith("fact")]) == 1


def test_bulk_tool_skips_before_provider_call(monkeypatch):
    called = []

    def fake_search_web(*args, **kwargs):
        called.append(args[0])
        return "duckduckgo", []

    monkeypatch.setattr(tools_search, "_search_web", fake_search_web)
    tool = tools_search.make_bulk_web_search_tool(
        context=lambda query: {
            "skip": query == "skip me",
            "skip_reason": "target reached",
            "max_results": 5,
        }
    )

    result = tool.invoke({"queries": ["skip me", "run me"]})
    statuses = {row["query"]: row["status"] for row in result["results"]}

    assert statuses["skip me"] == "SKIPPED"
    assert "skip me" not in called
    assert "run me" in called

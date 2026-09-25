from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project, SearchQuery
from app.services import search_planning
from app.services.search_planning import detect_coverage_gaps, plan_gap_fill_queries
from app.topic_profile import heuristic_topic_profile


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="enchentes no Rio de Janeiro",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        project_type="GENERAL_TOPIC",
        execution_profile="MIDIATICO_SIMPLES",
        topic_profile={},
    )
    session.add(project)
    session.commit()
    return session, project


def _valid_item(session, project, domain):
    session.add(
        MediaItem(
            project_id=project.id,
            title="Materia",
            url=f"https://{domain}/materia",
            canonical_url=f"https://{domain}/materia",
            domain=domain,
            status="VALID",
            fact_status="PENDING",
        )
    )
    session.commit()


def test_detect_lists_uncovered_portals_in_priority_order():
    session, project = _db()
    _valid_item(session, project, "g1.globo.com")

    gaps = detect_coverage_gaps(session, project)

    assert gaps["needs_fill"] is True
    portals = [entry["portal"] for entry in gaps["uncovered_portals"]]
    assert "G1/Globo" not in portals
    assert "YouTube" not in portals
    assert portals[0] == "O Globo"
    session.close()


def test_detect_zero_corpus_requires_recovery_even_without_portal_checks(monkeypatch):
    import importlib

    metrics_module = importlib.import_module("app.services.metrics")
    session, project = _db()
    monkeypatch.setattr(
        metrics_module, "metrics",
        lambda *_a, **_k: {"portal_checks": [], "valid_items": 0},
    )

    gaps = detect_coverage_gaps(session, project)

    assert gaps["needs_fill"] is True
    assert gaps["zero_corpus"] is True
    assert gaps["uncovered_portals"] == []
    session.close()


class _FakeGapAgent:
    def __init__(self, queries):
        self.queries = queries
        self.calls = 0

    def run(self, *, task, payload, **kwargs):
        assert task == "gap_planner"
        self.calls += 1
        assert payload["uncovered_portals"]
        return {"queries": self.queries}


def test_plan_accepts_only_new_open_queries(monkeypatch):
    session, project = _db()
    _valid_item(session, project, "g1.globo.com")
    session.add(
        SearchQuery(
            project_id=project.id,
            query="enchentes Rio de Janeiro resumo",
            kind="media_primary",
            purpose="MEDIA_REPERCUSSION",
            rationale="executada",
            priority=1,
        )
    )
    session.commit()
    agent = _FakeGapAgent([
        {"query": "enchentes Rio de Janeiro abrigos", "focus": "rotas de fuga", "rationale": "angulo novo"},
        {"query": "enchentes Rio de Janeiro resumo", "focus": "x", "rationale": "duplicata exata"},
        {"query": "Rio de Janeiro enchentes resumo", "focus": "x", "rationale": "parafrase"},
        {"query": "site:oglobo.globo.com enchentes Rio de Janeiro", "focus": "O Globo", "rationale": "site restrito"},
        {"query": "receita de bolo", "focus": "x", "rationale": "sem ancora"},
    ])
    monkeypatch.setattr(search_planning, "get_report_agent", lambda: agent)

    gaps = detect_coverage_gaps(session, project)
    created = plan_gap_fill_queries(session, project, gaps, max_queries=1)

    assert agent.calls == 1
    assert [row.query for row in created] == ["enchentes Rio de Janeiro abrigos"]
    row = created[0]
    assert row.kind == "media_complementary"
    assert row.purpose == "MEDIA_REPERCUSSION"

    # Segunda chamada nao duplica nada da primeira.
    agent2 = _FakeGapAgent([
        {"query": "enchentes Rio de Janeiro abrigos", "focus": "x", "rationale": "x"},
    ])
    monkeypatch.setattr(search_planning, "get_report_agent", lambda: agent2)
    second = [row.query for row in plan_gap_fill_queries(session, project, gaps, max_queries=4)]
    assert "enchentes Rio de Janeiro abrigos" not in second
    session.close()


def test_plan_fallback_mines_unused_profile_angles(monkeypatch):
    session, project = _db()
    _valid_item(session, project, "g1.globo.com")
    project.topic_profile = {"subject_terms": ["deslizamentos", "abrigos emergenciais"]}
    session.add(
        SearchQuery(
            project_id=project.id,
            query="enchentes no Rio de Janeiro",
            kind="media_primary",
            purpose="MEDIA_REPERCUSSION",
            rationale="executada",
            priority=1,
        )
    )
    session.commit()
    monkeypatch.setattr(search_planning, "llm_is_configured", lambda: False)

    gaps = detect_coverage_gaps(session, project)
    created = plan_gap_fill_queries(session, project, gaps, max_queries=2)

    queries = [row.query for row in created]
    assert queries == ["deslizamentos", "abrigos emergenciais"]
    assert all("site:" not in query for query in queries)
    stored = session.scalars(select(SearchQuery.query)).all()
    assert len(stored) == 3  # 1 executada + 2 complementares
    session.close()


def test_zero_corpus_recovery_uses_canonical_location_and_journalistic_terms(monkeypatch):
    session, project = _db()
    topic = "produção habitacional milícia mazuema"
    project.topic = topic
    project.topic_profile = heuristic_topic_profile(topic)
    session.add(
        SearchQuery(
            project_id=project.id,
            query="produção habitacional milícia Muzema",
            kind="media_primary",
            purpose="MEDIA_REPERCUSSION",
            rationale="primeira rodada",
            priority=1,
            execution_status="NO_RESULTS",
        )
    )
    session.commit()

    monkeypatch.setattr(search_planning, "llm_is_configured", lambda: False)
    gaps = {
        "uncovered_portals": [],
        "valid_items": 0,
        "target_items": 5,
        "zero_corpus": True,
        "needs_fill": True,
    }
    created = plan_gap_fill_queries(session, project, gaps, max_queries=3)

    queries = [row.query for row in created]
    assert queries
    assert all(row.kind == "media_zero_recovery" for row in created)
    assert any("Muzema" in query for query in queries)
    assert any(
        term in " ".join(queries).lower()
        for term in ("imoveis", "construcao", "mercado imobiliario", "moradia")
    )
    assert all("[zero-corpus recovery]" in row.rationale for row in created)
    session.close()

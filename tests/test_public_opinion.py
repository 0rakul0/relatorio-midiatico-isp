from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Project, PublicOpinionSurvey
from app.services import public_opinion as opinion


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _project(db):
    project = Project(
        topic="polarização política no Brasil em 2026",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={
            "subject_terms": ["polarização política", "eleições 2026"],
            "search_synonyms": ["polarização eleitoral Brasil"],
        },
        execution_options={},
        execution_plan={},
    )
    db.add(project)
    db.commit()
    return project


def _settings():
    return SimpleNamespace(
        public_opinion_enabled=True,
        public_opinion_max_queries=4,
        public_opinion_results_per_query=6,
        public_opinion_max_documents=12,
        public_opinion_fetch_max_chars=18000,
        public_opinion_fetch_timeout_seconds=10.0,
        public_opinion_llm_max_chars=14000,
    )


def test_public_opinion_queries_are_separate_from_social_comment_search(monkeypatch):
    db = _session()
    project = _project(db)
    monkeypatch.setattr(opinion, "get_settings", _settings)

    queries = opinion._queries(project)

    assert queries
    assert all("pesquisa" in query.lower() or "levantamento" in query.lower() for query in queries)
    assert all("site:instagram.com" not in query for query in queries)
    assert all("site:x.com" not in query for query in queries)
    db.close()


def test_public_opinion_persists_structured_survey(monkeypatch):
    db = _session()
    project = _project(db)
    monkeypatch.setattr(opinion, "get_settings", _settings)
    monkeypatch.setattr(opinion, "llm_is_configured", lambda: True)
    monkeypatch.setattr(
        opinion,
        "discover_public_opinion_sources",
        lambda **_kwargs: [{
            "title": "Pesquisa mede opinião sobre polarização",
            "url": "https://example.org/pesquisa-polarizacao",
            "snippet": "Foram entrevistadas 2.000 pessoas.",
            "published_at": "2026-10-01",
            "provider": "duckduckgo_text",
        }],
    )
    monkeypatch.setattr(
        opinion,
        "hydrate_public_opinion_source",
        lambda *_args, **_kwargs: (
            "O Instituto Exemplo entrevistou 2.000 eleitores brasileiros entre "
            "28 e 30 de setembro de 2026. Margem de erro de 2 pontos percentuais. "
            "Quarenta e três por cento disseram estar cansados da polarização."
        ),
    )

    class FakeAgent:
        def run(self, **kwargs):
            assert kwargs["task"] == "public_opinion_extraction"
            return {
                "is_public_opinion_research": True,
                "institute": "Instituto Exemplo",
                "sponsor": None,
                "population": "eleitores brasileiros",
                "geography": "Brasil",
                "field_start": "2026-09-28",
                "field_end": "2026-09-30",
                "publication_date": "2026-10-01",
                "sample_size": 2000,
                "margin_of_error": "2 pontos percentuais",
                "confidence_level": None,
                "methodology": "entrevistas com eleitores",
                "sampling_method": None,
                "representative_scope": "eleitores brasileiros",
                "caveats": None,
                "indicators": [{
                    "label": "cansaço com polarização",
                    "question": None,
                    "value": "43",
                    "unit": "%",
                    "subgroup": "total da amostra",
                    "evidence": "43% disseram estar cansados da polarização",
                }],
                "evidence_summary": "Pesquisa nacional com 2.000 eleitores.",
            }

    monkeypatch.setattr(opinion, "get_report_agent", lambda: FakeAgent())

    result = opinion.collect_public_opinion(db, project)

    assert result["status"] == "COMPLETED"
    assert result["count"] == 1
    row = db.scalar(select(PublicOpinionSurvey))
    assert row is not None
    assert row.institute == "Instituto Exemplo"
    assert row.sample_size == 2000
    assert row.population == "eleitores brasileiros"
    assert row.indicators[0]["value"] == "43"
    db.close()


def test_social_or_open_poll_is_not_persisted_as_public_opinion(monkeypatch):
    db = _session()
    project = _project(db)
    monkeypatch.setattr(opinion, "get_settings", _settings)
    monkeypatch.setattr(opinion, "llm_is_configured", lambda: True)
    monkeypatch.setattr(
        opinion,
        "discover_public_opinion_sources",
        lambda **_kwargs: [{
            "title": "Enquete aberta em rede social",
            "url": "https://example.org/enquete",
            "snippet": "Usuários votaram espontaneamente.",
            "provider": "duckduckgo_text",
        }],
    )
    monkeypatch.setattr(
        opinion,
        "hydrate_public_opinion_source",
        lambda *_args, **_kwargs: "Enquete aberta: usuários clicaram para votar espontaneamente.",
    )

    class FakeAgent:
        def run(self, **_kwargs):
            return {
                "is_public_opinion_research": False,
                "institute": None,
                "sponsor": None,
                "population": None,
                "geography": None,
                "field_start": None,
                "field_end": None,
                "publication_date": None,
                "sample_size": None,
                "margin_of_error": None,
                "confidence_level": None,
                "methodology": None,
                "sampling_method": None,
                "representative_scope": None,
                "caveats": "enquete espontânea",
                "indicators": [],
                "evidence_summary": None,
            }

    monkeypatch.setattr(opinion, "get_report_agent", lambda: FakeAgent())

    result = opinion.collect_public_opinion(db, project)

    assert result["count"] == 0
    assert db.scalar(select(PublicOpinionSurvey)) is None
    db.close()

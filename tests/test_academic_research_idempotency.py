from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AcademicPaper, Project
from app.services import academic_research


class _FakeAgent:
    def __init__(self, paper):
        self.paper = paper

    def run(self, **_kwargs):
        # Reproduz o bug: a selecao estruturada devolve duas vezes o mesmo
        # provider/external_id na mesma chamada.
        return {
            "searched": True,
            "queries": ["crime rio de janeiro"],
            "summary": "contexto",
            "papers": [dict(self.paper), dict(self.paper)],
        }


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _project(db):
    project = Project(
        topic="criminalidade no Rio de Janeiro",
        institution="Instituto de Segurança Pública",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 12, 31),
        project_type="GENERAL_TOPIC",
        topic_profile={},
        execution_profile="AUTO",
        execution_options={},
        execution_plan={},
        has_custom_date_window=True,
        status="DRAFT",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_academic_persistence_deduplicates_same_selection_with_autoflush_disabled(monkeypatch):
    db = _db()
    project = _project(db)

    raw = {
        "provider": "scielo",
        "external_id": "10.1590/2175-3369.015.e20220141",
        "doi": "10.1590/2175-3369.015.e20220141",
        "title": "Criminalidade e espaço urbano",
        "abstract": "Resumo do artigo.",
        "authors": ["Autor A"],
        "published_at": "2023-01-01",
        "updated_at": None,
        "categories": [],
        "url": "https://doi.org/10.1590/2175-3369.015.e20220141",
        "pdf_url": None,
        "journal_reference": "urbe",
        "is_preprint": False,
    }
    selected = {
        "provider": "scielo",
        "external_id": raw["external_id"],
        "title": raw["title"],
        "title_ptbr": raw["title"],
        "abstract_ptbr": raw["abstract"],
        "original_language": "pt",
        "relevance_score": 0.95,
        "relation_to_topic": "Contextualiza criminalidade urbana no Rio de Janeiro.",
    }

    def fake_tools(*, academic_sink=None, **_kwargs):
        academic_sink([raw], "scielo")
        return []

    monkeypatch.setattr(academic_research, "build_agent_tools", fake_tools)
    monkeypatch.setattr(
        academic_research,
        "get_report_agent",
        lambda: _FakeAgent(selected),
    )

    first = academic_research.research_academic_literature(db, project)
    second = academic_research.research_academic_literature(db, project)

    count = db.scalar(
        select(func.count(AcademicPaper.id)).where(
            AcademicPaper.project_id == project.id
        )
    )
    rows = db.scalars(
        select(AcademicPaper).where(AcademicPaper.project_id == project.id)
    ).all()

    assert count == 1
    assert len(rows) == 1
    assert first["duplicates_skipped"] == 1
    assert second["duplicates_skipped"] == 1
    assert rows[0].provider == "scielo"
    assert rows[0].external_id == raw["external_id"]
    db.close()

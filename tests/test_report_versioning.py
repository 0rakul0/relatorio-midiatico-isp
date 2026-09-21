from datetime import date

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GeneratedReport, Project, ReportVersion
from app.report_qa import run_report_qa
from app.services.cache import cached_report_for_topic
from app.services.reporting import _save_report


def _project(session) -> Project:
    project = Project(
        topic="tema versionado",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        project_type="EVENT_TOPIC",
        topic_profile={"actors": [], "actions": [], "locations": []},
    )
    session.add(project)
    session.commit()
    return project


def _grounding() -> dict:
    return {"metrics": {"valid_items": 0}, "fact_events": [], "fact_evidence": []}


def test_save_report_is_versioned_immutable_and_qa_binds_to_version():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = _project(session)

    _save_report(session, project, {"title": "Rascunho"}, _grounding())

    saved = session.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    assert saved.version_no == 1
    assert saved.current_version_id is not None
    assert saved.content_hash
    assert saved.request_fingerprint
    assert session.scalar(select(func.count()).select_from(ReportVersion)) == 1

    # Mesmo conteúdo estrutural => mesma versão, sem linha nova.
    _save_report(session, project, {"title": "Rascunho"}, _grounding())
    assert session.scalar(select(func.count()).select_from(ReportVersion)) == 1

    # Conteúdo novo => versão 2 imutável.
    _save_report(session, project, {"title": "Revisado"}, _grounding())
    assert saved.version_no == 2
    assert session.scalar(select(func.count()).select_from(ReportVersion)) == 2
    v1 = session.scalar(
        select(ReportVersion).where(ReportVersion.version_no == 1)
    )
    assert v1.body["report"]["title"] == "Rascunho"

    qa = run_report_qa(session, project, saved.body)
    assert qa["approved"] is True
    active_version = session.get(ReportVersion, saved.current_version_id)
    assert active_version.qa_status == "APPROVED"
    assert active_version.qa_findings == saved.qa_findings
    session.close()


def test_cache_is_rejected_when_request_fingerprint_changes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = _project(session)

    _save_report(session, project, {"title": "Rascunho"}, _grounding())

    cached = cached_report_for_topic(
        session,
        "tema versionado",
        project.collection_start,
        project.collection_end,
    )
    assert cached is not None
    assert cached["version_no"] == 1

    project.topic_profile = {"actors": ["novo ator"], "actions": [], "locations": []}
    session.commit()
    assert (
        cached_report_for_topic(
            session,
            "tema versionado",
            project.collection_start,
            project.collection_end,
        )
        is None
    )
    session.close()
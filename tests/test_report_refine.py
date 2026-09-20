from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.report_qa as report_qa
from app.database import Base
from app.models import GeneratedReport, Project
from app.services import reporting

BAD = "não houve cobertura"
FIXED = "nenhum item validado na amostra"


def _base_report(summary):
    return {
        "title": "t", "interpretive_title": "", "subtitle": "",
        "executive_summary": summary, "fact_layer_intro": "", "opening": "",
        "panorama": "", "dominant_framing": "", "highest_yield": "",
        "institutional_narrative": "", "synthesis": "",
        "methodological_note": "", "thematic_axes": [], "risk_assessment": [],
        "recommendations": [], "press_kit": [],
    }


class _FakeAgent:
    def __init__(self, revise_fn):
        self.tasks = []
        self.revise_fn = revise_fn

    def run(self, *, task, payload, response_model=None, **kwargs):
        self.tasks.append(task)
        if task == "report_writer":
            return _base_report(BAD)
        if task == "report_reviser":
            return self.revise_fn(payload)
        if task == "qa":
            return {"findings": []}
        raise AssertionError(task)


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema de teste",
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


def _patch(monkeypatch, agent):
    monkeypatch.setattr(reporting, "get_report_agent", lambda: agent)
    monkeypatch.setattr(report_qa, "get_report_agent", lambda: agent)
    monkeypatch.setattr(reporting, "llm_is_configured", lambda: True)
    monkeypatch.setattr(report_qa, "llm_is_configured", lambda: True)


def test_refine_loop_fixes_and_approves(monkeypatch):
    session, project = _db()
    agent = _FakeAgent(
        lambda payload: {**_base_report(FIXED), "previous_seen": True}
        if payload["blocking_findings"] else _base_report(BAD)
    )
    _patch(monkeypatch, agent)

    drafted = reporting.draft_report_with_llm(session, project)
    qa = report_qa.run_report_qa(session, project, drafted)
    assert qa["approved"] is False

    drafted, qa, refinements = reporting.refine_report_with_qa(
        session, project, drafted, qa
    )

    assert refinements == 1
    assert qa["approved"] is True
    assert agent.tasks.count("report_reviser") == 1
    saved = session.query(GeneratedReport).filter_by(project_id=project.id).one()
    assert FIXED in (saved.body["report"]["executive_summary"] or "")
    session.close()


def test_refine_loop_respects_cap(monkeypatch):
    session, project = _db()
    agent = _FakeAgent(lambda payload: _base_report(BAD))
    _patch(monkeypatch, agent)

    drafted = reporting.draft_report_with_llm(session, project)
    qa = report_qa.run_report_qa(session, project, drafted)

    _, qa, refinements = reporting.refine_report_with_qa(session, project, drafted, qa)

    assert refinements == 2  # default MAX_QA_REFINEMENTS
    assert agent.tasks.count("report_reviser") == 2
    assert qa["approved"] is False
    session.close()


def test_refine_loop_disabled_when_max_is_zero(monkeypatch):
    session, project = _db()
    agent = _FakeAgent(lambda payload: _base_report(FIXED))
    _patch(monkeypatch, agent)
    monkeypatch.setattr(
        reporting, "get_settings", lambda: SimpleNamespace(max_qa_refinements=0)
    )

    drafted = reporting.draft_report_with_llm(session, project)
    qa = report_qa.run_report_qa(session, project, drafted)

    _, _, refinements = reporting.refine_report_with_qa(session, project, drafted, qa)

    assert refinements == 0
    assert "report_reviser" not in agent.tasks
    session.close()

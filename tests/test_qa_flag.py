from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Project, SearchQuery
from app.services import reporting  # noqa: F401  (garante pacote carregado)
import app.report_qa as report_qa


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={},
    )
    session.add(project)
    session.commit()
    return session, project


def _payload():
    return {
        "report": {"title": "t", "executive_summary": "ok"},
        "metrics": {"valid_items": 0},
        "fact_events": [],
        "project": {"project_type": "GENERAL_TOPIC"},
        "corpus": [],
    }


def test_llm_qa_disabled_skips_agent_and_marks_finding(monkeypatch):
    session, project = _session()
    monkeypatch.setattr(
        report_qa, "get_settings", lambda: SimpleNamespace(enable_llm_qa=False)
    )

    def _boom(**_kwargs):
        raise AssertionError("agente nao deveria ser chamado")

    monkeypatch.setattr(report_qa, "get_report_agent", lambda: SimpleNamespace(run=_boom))

    result = report_qa.run_report_qa(session, project, _payload())

    codes = [f["code"] for f in result["findings"]]
    assert "LLM_QA_DISABLED" in codes
    assert result["approved"] is True
    session.close()


def test_llm_qa_enabled_calls_agent(monkeypatch):
    session, project = _session()
    monkeypatch.setattr(
        report_qa, "get_settings", lambda: SimpleNamespace(enable_llm_qa=True)
    )
    monkeypatch.setattr(report_qa, "llm_is_configured", lambda: True)
    calls = []
    monkeypatch.setattr(
        report_qa,
        "get_report_agent",
        lambda: SimpleNamespace(run=lambda **_kw: (calls.append(1), {"findings": []})[1]),
    )

    result = report_qa.run_report_qa(session, project, _payload())

    assert calls, "agente deveria ser chamado"
    assert all(f["code"] != "LLM_QA_DISABLED" for f in result["findings"])
    session.close()


def test_zero_media_with_context_and_no_expansion_is_blocked(monkeypatch):
    session, project = _session()
    monkeypatch.setattr(
        report_qa, "get_settings", lambda: SimpleNamespace(enable_llm_qa=False)
    )
    payload = _payload()
    payload["academic_papers"] = [
        {
            "title": "Contexto acadêmico relacionado",
            "url": "https://example.org/paper",
        }
    ]

    result = report_qa.run_report_qa(session, project, payload)

    codes = {finding["code"] for finding in result["findings"]}
    assert "ZERO_MEDIA_WITHOUT_SEARCH_EXPANSION" in codes
    assert result["approved"] is False
    session.close()


def test_zero_media_with_context_is_allowed_after_auditable_recovery(monkeypatch):
    session, project = _session()
    monkeypatch.setattr(
        report_qa, "get_settings", lambda: SimpleNamespace(enable_llm_qa=False)
    )
    session.add(
        SearchQuery(
            project_id=project.id,
            query='"Muzema" milicia imoveis',
            kind="media_zero_recovery",
            purpose="MEDIA_REPERCUSSION",
            rationale="[zero-corpus recovery] tentativa ampliada",
            priority=1,
            executed_at=datetime.now(),
            execution_status="NO_RESULTS",
        )
    )
    session.commit()

    payload = _payload()
    payload["academic_papers"] = [
        {
            "title": "Contexto acadêmico relacionado",
            "url": "https://example.org/paper",
        }
    ]

    result = report_qa.run_report_qa(session, project, payload)

    codes = {finding["code"] for finding in result["findings"]}
    assert "ZERO_MEDIA_WITHOUT_SEARCH_EXPANSION" not in codes
    assert result["approved"] is True
    session.close()

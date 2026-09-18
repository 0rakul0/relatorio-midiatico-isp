from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Project


def test_profile_discovery_delegates_external_search_to_single_agent_run(monkeypatch):
    from app.services import project_profile

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="Dossiê Mulher 2026",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 1, 31),
    )
    session.add(project)
    session.commit()

    runs = []

    def fake_run(*, task, **kwargs):
        runs.append((task, kwargs.get("max_tool_rounds"), kwargs.get("tools")))
        return {
            "institution": None,
            "product_status": "PUBLISHED",
            "product_evidence": "Produto publicado.",
            "product_source_index": 0,
            "launch_status": "NOT_FOUND",
            "launch_date": None,
            "expected_launch_date": None,
            "launch_evidence": None,
            "launch_source_index": None,
            "official_facts": [],
        }

    monkeypatch.setattr(
        project_profile,
        "get_settings",
        lambda: SimpleNamespace(max_agent_tool_rounds=3),
    )
    monkeypatch.setattr(project_profile, "llm_is_configured", lambda: True)
    # O teto de buscas externas vive no agente, não no chamador: a discover não
    # pesquisa por conta própria (agente decide; a tool pesquisar_internet executa).
    monkeypatch.setattr(project_profile, "build_agent_tools", lambda **_: ["tool-de-exemplo"])
    monkeypatch.setattr(project_profile, "get_report_agent", lambda: SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        project_profile,
        "build_topic_profile",
        lambda _topic: {"project_type": "INSTITUTIONAL_PRODUCT", "product_name": "Dossiê Mulher 2026"},
    )

    result = project_profile.discover_project_profile(session, project)

    assert [task for task, *_ in runs] == ["documentalist"]
    assert runs[0][1] == 3  # max_tool_rounds repassado ao agente
    assert runs[0][2] == ["tool-de-exemplo"]
    assert result["discovery_stats"]["agent_optional_search_calls"] == 0
    assert result["product_status"] == "PUBLISHED"
    session.close()
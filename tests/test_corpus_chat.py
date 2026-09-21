from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import GeneratedReport, MediaItem, Project
from app.services.corpus_chat import (
    _chat_corpus_members,
    chat_with_all_corpus,
    chat_with_corpus,
    list_chat_projects,
)


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _project(session, topic, owner="u1", created_days_ago=1, report_days_ago=None) -> Project:
    project = Project(
        topic=topic,
        owner_id=owner,
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        project_type="EVENT_TOPIC",
        topic_profile={"actors": [], "actions": [], "locations": []},
        created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
    )
    session.add(project)
    session.flush()
    if report_days_ago is not None:
        session.add(GeneratedReport(
            project_id=project.id,
            body={},
            generated_at=datetime.now(timezone.utc) - timedelta(days=report_days_ago),
        ))
    return project


def _valid_item(session, project, title="materia", days_ago=0, status="VALID", content="texto") -> MediaItem:
    item = MediaItem(
        project_id=project.id,
        title=title,
        url=f"https://example.com/{title.replace(' ', '-')}",
        canonical_url=f"https://example.com/{title.replace(' ', '-')}",
        domain="example.com",
        status=status,
        content=content,
        media_origin="PORTAL_NOTICIAS",
        search_source="ddgs",
        published_at=date(2026, 8, 20) - timedelta(days=days_ago),
    )
    session.add(item)
    return item


def test_list_chat_projects_orders_by_report_and_picks_last_active():
    session = _session()
    p1 = _project(session, "tema com relatorio", report_days_ago=10)
    p3 = _project(session, "tema outro relatorio", report_days_ago=9, created_days_ago=2)
    p2 = _project(session, "tema sem relatorio", created_days_ago=1)
    _valid_item(session, p1, "a1")
    _valid_item(session, p1, "a2")
    _valid_item(session, p1, "descarte", status="REJECTED")
    _valid_item(session, p3, "b1")
    session.commit()

    user = SimpleNamespace(id="u1", is_admin=False)
    result = list_chat_projects(session, user)

    assert result["last_active_project_id"] == p3.id
    assert [row["id"] for row in result["projects"]] == [p3.id, p1.id, p2.id]
    by_id = {row["id"]: row for row in result["projects"]}
    assert by_id[p1.id]["valid_items"] == 2
    assert by_id[p3.id]["valid_items"] == 1
    assert by_id[p2.id]["valid_items"] == 0
    assert by_id[p1.id]["generated_at"] is not None
    assert by_id[p2.id]["generated_at"] is None
    assert result["all_corpus"]["valid_items"] == 3
    assert result["all_corpus"]["project_count"] == 2
    session.close()


def test_list_chat_projects_scopes_by_owner():
    session = _session()
    mine = _project(session, "meu", owner="u1")
    _project(session, "alheio", owner="outro")
    session.commit()

    user = SimpleNamespace(id="u1", is_admin=False)
    result = list_chat_projects(session, user)
    assert [row["id"] for row in result["projects"]] == [mine.id]

    admin = SimpleNamespace(id="u1", is_admin=True)
    result = list_chat_projects(session, admin)
    assert len(result["projects"]) == 2
    session.close()


def test_chat_corpus_members_only_valid_sorted_by_date_and_truncated():
    session = _session()
    project = _project(session, "tema")
    older = _valid_item(session, project, "mais antiga", days_ago=9)
    newer = _valid_item(session, project, "mais nova", days_ago=1)
    _valid_item(session, project, "descarte", status="REJECTED")
    session.commit()

    members = _chat_corpus_members(session, project)
    assert [m["id"] for m in members] == [newer.id, older.id]
    assert members[0]["published_at"] == (date(2026, 8, 20) - timedelta(days=1)).isoformat()
    assert members[0]["search_source"] == "ddgs"

    long_item = _valid_item(session, project, "longo", days_ago=0, content="x" * 5000)
    session.commit()
    members = _chat_corpus_members(session, project, limit=10)
    assert len(members) == 3
    assert len(members[0]["content"]) <= 3000
    assert long_item.id in [m["id"] for m in members]
    session.close()


def test_chat_with_corpus_returns_answer_and_mapped_sources(monkeypatch):
    session = _session()
    project = _project(session, "tema")
    i0 = _valid_item(session, project, "fonte zero", days_ago=3)
    _valid_item(session, project, "fonte um", days_ago=2)
    i2 = _valid_item(session, project, "fonte dois", days_ago=1)
    session.commit()

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"answer": "resposta", "used_member_indices": [2, 0]}

    monkeypatch.setattr("app.services.corpus_chat.llm_is_configured", lambda: True)
    fake_agent = SimpleNamespace(run=fake_run)
    monkeypatch.setattr("app.services.corpus_chat.get_report_agent", lambda: fake_agent)

    result = chat_with_corpus(session, project, [{"role": "user", "content": "oi"}])

    assert result["answer"] == "resposta"
    assert [s["url"] for s in result["sources"]] == [i0.url, i2.url]
    assert result["sources"][0]["title"] == "fonte zero"
    assert result["corpus_size"] == 3
    assert captured["task"] == "chat"
    assert captured["tools"] is None
    payload = captured["payload"]
    assert payload["project"]["topic"] == "tema"
    assert len(payload["corpus"]) == 3
    assert payload["conversation"][0]["content"] == "oi"
    session.close()


def test_chat_with_corpus_ignores_unknown_indices(monkeypatch):
    session = _session()
    project = _project(session, "tema")
    _valid_item(session, project, "fonte")
    session.commit()

    def fake_run(**kwargs):
        return {"answer": "sem fonte", "used_member_indices": [99]}

    monkeypatch.setattr("app.services.corpus_chat.llm_is_configured", lambda: True)
    monkeypatch.setattr("app.services.corpus_chat.get_report_agent", lambda: SimpleNamespace(run=fake_run))

    result = chat_with_corpus(session, project, [{"role": "user", "content": "oi"}])
    assert result["sources"] == []
    assert result["corpus_size"] == 1
    session.close()


def test_chat_with_corpus_requires_llm_configuration(monkeypatch):
    session = _session()
    project = _project(session, "tema")
    session.commit()
    monkeypatch.setattr("app.services.corpus_chat.llm_is_configured", lambda: False)

    with pytest.raises(RuntimeError) as excinfo:
        chat_with_corpus(session, project, [{"role": "user", "content": "oi"}])
    assert "OPENAI_API_KEY" in str(excinfo.value)
    session.close()

def test_chat_retrieval_prefers_question_terms():
    session = _session()
    project = _project(session, "tema")
    relevant = _valid_item(
        session,
        project,
        "violencia digital contra mulheres",
        days_ago=8,
        content="A materia discute violencia digital, ameacas e perseguição contra mulheres.",
    )
    _valid_item(
        session,
        project,
        "seguranca publica geral",
        days_ago=0,
        content="Texto sobre outro assunto de seguranca.",
    )
    session.commit()

    members = _chat_corpus_members(
        session,
        project,
        limit=1,
        question="O que apareceu sobre violencia digital contra mulheres?",
    )
    assert len(members) == 1
    assert members[0]["id"] == relevant.id
    session.close()


def test_chat_with_all_corpus_is_scoped_to_visible_projects(monkeypatch):
    session = _session()
    mine = _project(session, "meu tema", owner="u1")
    other = _project(session, "tema alheio", owner="u2")
    mine_item = _valid_item(session, mine, "minha fonte")
    _valid_item(session, other, "fonte alheia")
    session.commit()

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"answer": "resposta global", "used_member_indices": [0]}

    monkeypatch.setattr("app.services.corpus_chat.llm_is_configured", lambda: True)
    monkeypatch.setattr(
        "app.services.corpus_chat.get_report_agent",
        lambda: SimpleNamespace(run=fake_run),
    )

    user = SimpleNamespace(id="u1", is_admin=False)
    result = chat_with_all_corpus(
        session,
        user,
        [{"role": "user", "content": "minha fonte"}],
    )

    assert result["corpus_size"] == 1
    assert result["sources"][0]["url"] == mine_item.url
    assert captured["payload"]["project"]["scope"] == "ALL"
    session.close()

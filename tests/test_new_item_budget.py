from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project, SearchHit, SearchQuery
from app.services.collection import persist


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _project(session):
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={},
    )
    session.add(project)
    session.commit()
    return project


def _query(session, project):
    query = SearchQuery(
        project_id=project.id,
        query='"tema de teste"',
        kind="media_primary",
        purpose="MEDIA_REPERCUSSION",
        rationale="teste",
        priority=1,
    )
    session.add(query)
    session.commit()
    return query


def _row(url, title="Materia"):
    return {
        "title": title,
        "url": url,
        "snippet": "texto",
        "content": "texto",
        "published_at": "2026-08-10",
        "source_name": "Portal",
        "provider": "duckduckgo",
    }


def test_new_item_budget_preserves_hit_without_creating_media_item(monkeypatch):
    session = _db()
    project = _project(session)
    query = _query(session, project)
    monkeypatch.setattr(persist, "collection_guard", lambda *_a, **_k: True)
    monkeypatch.setattr(
        persist, "get_settings", lambda: SimpleNamespace(max_new_media_items=1)
    )

    counts, _usable = persist.persist_web_rows(
        session, project, query, {},
        [_row("https://news.example.com/a"), _row("https://news.example.com/b")],
        has_window=False,
    )
    session.commit()

    assert counts["added"] == 1
    assert counts["over_budget"] == 1
    assert counts["persisted"] == 2
    assert len(session.scalars(select(MediaItem)).all()) == 1

    hits = session.scalars(select(SearchHit).order_by(SearchHit.id)).all()
    assert len(hits) == 2
    assert hits[0].media_item_id is not None
    assert hits[1].media_item_id is None
    assert "OVER_NEW_ITEM_BUDGET" in hits[1].technical_flags


def test_new_item_budget_ignores_duplicates_and_reused(monkeypatch):
    session = _db()
    project = _project(session)
    query = _query(session, project)
    monkeypatch.setattr(persist, "collection_guard", lambda *_a, **_k: True)
    monkeypatch.setattr(
        persist, "get_settings", lambda: SimpleNamespace(max_new_media_items=1)
    )

    # Item reutilizado do historico nao consome o teto de itens novos.
    session.add(
        MediaItem(
            project_id=project.id,
            title="Antiga",
            url="https://old.example.com/a",
            canonical_url="https://old.example.com/a",
            domain="old.example.com",
            corpus_origin="REUSED",
            search_source="corpus_reuse",
            status="PENDING",
            fact_status="PENDING",
        )
    )
    session.commit()
    existing = {
        item.canonical_url: item
        for item in session.scalars(
            select(MediaItem).where(MediaItem.project_id == project.id)
        ).all()
    }

    counts, _usable = persist.persist_web_rows(
        session, project, query, existing,
        [_row("https://news.example.com/nova")],
        has_window=False,
    )
    assert counts["added"] == 1
    assert counts["over_budget"] == 0

    # Duplicata de URL ja consolidada nao consome teto nem cria item.
    counts, _usable = persist.persist_web_rows(
        session, project, query, existing,
        [_row("https://news.example.com/nova", title="Repetida")],
        has_window=False,
    )
    assert counts["added"] == 0
    assert counts["duplicates"] == 1
    assert counts["over_budget"] == 0
    session.close()

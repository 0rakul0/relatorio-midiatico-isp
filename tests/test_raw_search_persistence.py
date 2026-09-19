from datetime import date

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
        event_start=date(2026, 8, 1),
        event_end=date(2026, 8, 31),
        has_custom_date_window=True,
        project_type="EVENT_TOPIC",
        topic_profile={},
    )
    session.add(project)
    session.commit()
    return project


def test_collection_preserves_flagged_hits_instead_of_discarding(monkeypatch):
    session = _db()
    project = _project(session)
    query = SearchQuery(
        project_id=project.id,
        query='site:example.com "tema de teste"',
        kind="media_portal",
        purpose="MEDIA_REPERCUSSION",
        rationale="teste",
        priority=2,
    )
    session.add(query)
    session.commit()

    monkeypatch.setattr(persist, "collection_guard", lambda *_a, **_k: False)

    rows = [
        {
            "title": "Resultado fora do dominio e da janela",
            "url": "https://other.example.org/materia?id=7",
            "snippet": "texto retornado pelo buscador",
            "content": "texto retornado pelo buscador",
            "published_at": "2026-07-20",
            "source_name": "Outro portal",
            "provider": "duckduckgo",
        }
    ]

    counts, usable = persist.persist_web_rows(
        session,
        project,
        query,
        {},
        rows,
        has_window=True,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    session.commit()

    hits = session.scalars(select(SearchHit)).all()
    items = session.scalars(select(MediaItem)).all()

    assert counts["returned"] == 1
    assert counts["persisted"] == 1
    assert counts["rejected"] == 0
    assert usable == []
    assert len(hits) == 1
    assert len(items) == 1
    assert hits[0].media_item_id == items[0].id
    assert hits[0].technical_status == "FLAGGED"
    assert set(hits[0].technical_flags) >= {
        "DOMAIN_MISMATCH",
        "OUTSIDE_COLLECTION_WINDOW",
        "COLLECTION_GUARD_MISMATCH",
    }
    assert items[0].status == "PENDING"


def test_duplicate_discoveries_remain_as_two_hits_but_one_media_item(monkeypatch):
    session = _db()
    project = _project(session)
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
    monkeypatch.setattr(persist, "collection_guard", lambda *_a, **_k: True)

    existing = {}
    first = [{
        "title": "Materia",
        "url": "https://news.example.com/a?id=1&utm_source=x",
        "snippet": "A",
        "content": "A",
        "published_at": "2026-08-10",
        "source_name": "News",
        "provider": "duckduckgo",
    }]
    second = [{
        "title": "Materia novamente",
        "url": "https://news.example.com/a?id=1",
        "snippet": "B",
        "content": "B",
        "published_at": "2026-08-10",
        "source_name": "News",
        "provider": "duckduckgo",
    }]

    persist.persist_web_rows(
        session, project, query, existing, first,
        has_window=True, start=date(2026, 8, 1), end=date(2026, 8, 31),
    )
    persist.persist_web_rows(
        session, project, query, existing, second,
        has_window=True, start=date(2026, 8, 1), end=date(2026, 8, 31),
    )
    session.commit()

    hits = session.scalars(select(SearchHit).order_by(SearchHit.id)).all()
    items = session.scalars(select(MediaItem)).all()

    assert len(hits) == 2
    assert len(items) == 1
    assert hits[0].media_item_id == items[0].id
    assert hits[1].media_item_id == items[0].id
    assert "DUPLICATE_URL" in hits[1].technical_flags


def test_invalid_url_is_still_audited_without_creating_media_item(monkeypatch):
    session = _db()
    project = _project(session)
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
    monkeypatch.setattr(persist, "collection_guard", lambda *_a, **_k: True)

    counts, usable = persist.persist_web_rows(
        session,
        project,
        query,
        {},
        [{
            "title": "hit quebrado",
            "url": "javascript:alert(1)",
            "snippet": "resultado bruto",
            "provider": "duckduckgo",
        }],
        has_window=False,
    )
    session.commit()

    hit = session.scalar(select(SearchHit))
    assert counts["persisted"] == 1
    assert usable == []
    assert hit is not None
    assert hit.technical_status == "INVALID"
    assert "INVALID_URL" in hit.technical_flags
    assert session.scalars(select(MediaItem)).all() == []

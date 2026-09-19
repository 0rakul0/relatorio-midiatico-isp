from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project
from app.services.corpus_reuse import (
    _document_expired,
    reuse_prior_corpus,
)


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _project(session, topic="dossie mulher", window=False):
    project = Project(
        topic=topic,
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        has_custom_date_window=window,
        topic_profile={},
    )
    session.add(project)
    session.commit()
    return project


def test_document_expired_respects_window_exemption_and_unknown_dates():
    today = date(2026, 9, 19)

    class Doc:
        def __init__(self, published_at=None, first_seen_at=None):
            self.published_at = published_at
            self.first_seen_at = first_seen_at

    class WindowedProject:
        has_custom_date_window = True
        collection_start = date(2026, 8, 1)
        collection_end = date(2026, 8, 31)

    class OpenProject:
        has_custom_date_window = False
        collection_start = date(2026, 8, 1)
        collection_end = date(2026, 8, 31)

    old = Doc(published_at=date(2024, 1, 1))
    fresh = Doc(published_at=date(2026, 9, 1))
    undated = Doc()
    in_window = Doc(published_at=date(2026, 8, 15))

    assert _document_expired(old, OpenProject(), today, 180) is True
    assert _document_expired(fresh, OpenProject(), today, 180) is False
    assert _document_expired(undated, OpenProject(), today, 180) is False
    assert _document_expired(old, OpenProject(), today, 0) is False
    # Janela explicita protege documentos do periodo pedido.
    assert _document_expired(in_window, WindowedProject(), today, 180) is False
    assert _document_expired(old, WindowedProject(), today, 180) is True


def _seed_previous(session, published_at):
    previous = _project(session)
    session.add(
        MediaItem(
            project_id=previous.id,
            title="dossie mulher: materia sobre o tema",
            url="https://news.example.com/dossie",
            canonical_url="https://news.example.com/dossie",
            domain="news.example.com",
            published_at=published_at,
            snippet="dossie mulher em pauta",
            content="dossie mulher em pauta",
            source_name="Portal",
            search_source="duckduckgo",
            status="VALID",
            fact_status="PENDING",
        )
    )
    session.commit()
    return previous


def test_reuse_skips_expired_documents_but_keeps_fresh_ones():
    session = _db()
    _seed_previous(session, published_at=date(2024, 1, 1))
    current = _project(session)

    summary = reuse_prior_corpus(session, current)

    assert summary["enabled"] is True
    assert summary["reused"] == 0
    assert summary["expired_documents"] == 1
    session.close()


def test_reuse_keeps_documents_within_validity():
    session = _db()
    _seed_previous(session, published_at=date.today() - timedelta(days=10))
    current = _project(session)

    summary = reuse_prior_corpus(session, current)

    assert summary["reused"] == 1
    assert summary["expired_documents"] == 0
    session.close()

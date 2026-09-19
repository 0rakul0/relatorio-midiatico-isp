from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project, SearchHit, SearchQuery
from app.services.collection import persist
from app.services.collection.media_origin import (
    PORTAL_NOTICIAS,
    REDE_SOCIAL,
    YOUTUBE,
    classify_media_origin,
)
from app.services.metrics import split_corpus_by_origin


def test_classify_media_origin_separates_portal_social_youtube():
    assert classify_media_origin("https://g1.globo.com/materia") == PORTAL_NOTICIAS
    assert classify_media_origin("https://www.youtube.com/watch?v=abc") == YOUTUBE
    assert classify_media_origin("https://youtu.be/abc") == YOUTUBE
    assert classify_media_origin("https://www.instagram.com/p/abc") == REDE_SOCIAL
    assert classify_media_origin("https://x.com/perfil/status/1") == REDE_SOCIAL
    assert classify_media_origin("https://www.tiktok.com/@canal/video/1") == REDE_SOCIAL
    assert classify_media_origin("https://www.facebook.com/post/1") == REDE_SOCIAL
    assert classify_media_origin("https://evil-youtube.com/watch") == PORTAL_NOTICIAS
    assert classify_media_origin(None) == PORTAL_NOTICIAS


def test_persist_web_rows_store_media_origin(monkeypatch):
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

    rows = [
        {
            "title": "Portal",
            "url": "https://g1.globo.com/materia",
            "snippet": "A",
            "content": "A",
            "published_at": "2026-08-10",
            "source_name": "G1",
            "provider": "duckduckgo",
        },
        {
            "title": "Post",
            "url": "https://www.instagram.com/p/abc123/",
            "snippet": "B",
            "content": "B",
            "published_at": "2026-08-10",
            "source_name": "Instagram",
            "provider": "duckduckgo",
        },
        {
            "title": "Video",
            "url": "https://www.youtube.com/watch?v=abc123",
            "snippet": "C",
            "content": "C",
            "published_at": "2026-08-10",
            "source_name": "Canal",
            "provider": "duckduckgo_video",
        },
    ]
    persist.persist_web_rows(
        session, project, query, {}, rows, has_window=False,
    )
    session.commit()

    items = {
        item.url: item
        for item in session.scalars(select(MediaItem)).all()
    }
    assert items["https://g1.globo.com/materia"].media_origin == PORTAL_NOTICIAS
    assert items["https://www.instagram.com/p/abc123/"].media_origin == REDE_SOCIAL
    assert items["https://www.youtube.com/watch?v=abc123"].media_origin == YOUTUBE

    hits = session.scalars(select(SearchHit)).all()
    assert len(hits) == 3
    assert {hit.media_origin for hit in hits} == {PORTAL_NOTICIAS, REDE_SOCIAL, YOUTUBE}

    corpus = [
        {"url": item.url, "domain": item.domain, "media_origin": item.media_origin}
        for item in items.values()
    ]
    buckets = split_corpus_by_origin(corpus)
    assert len(buckets["portal_noticias"]) == 1
    assert len(buckets["redes_sociais"]) == 1
    assert len(buckets["youtube"]) == 1
    session.close()

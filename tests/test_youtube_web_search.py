from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project
from app.services import canonicalize, is_youtube_url, metrics
from app.source_registry import PRIORITY_YOUTUBE_CHANNELS


def test_youtube_url_validation_accepts_only_youtube_hosts():
    assert is_youtube_url("https://www.youtube.com/watch?v=abc123")
    assert is_youtube_url("https://youtu.be/abc123")
    assert not is_youtube_url("https://youtube.com.evil.example/watch?v=abc123")
    assert not is_youtube_url("https://example.com/watch?v=abc123")
    assert canonicalize("https://youtu.be/abc123") == canonicalize("https://www.youtube.com/watch?v=abc123")


def test_collect_youtube_uses_duckduckgo_and_rejects_external_urls(monkeypatch):
    from app import services

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={"actors": [], "actions": [], "locations": []},
    )
    session.add(project)
    session.commit()

    def fake_videos(query, **_kwargs):
        return [
            {
                "title": "Vídeo encontrado",
                "url": "https://www.youtube.com/watch?v=abc123&utm_source=test",
                "channel": "ISP RJ",
                "published_at": "2026-08-12",
                "description": "Descrição do vídeo",
                "view_count": 1_868,
                "provider": "duckduckgo_video",
            },
            {
                "title": "Resultado externo",
                "url": "https://example.com/video",
                "channel": None,
                "published_at": None,
                "description": None,
                "view_count": None,
                "provider": "duckduckgo_video",
            },
        ]

    monkeypatch.setattr(services, "duckduckgo_videos", fake_videos)
    assert services.collect_youtube_duckduckgo(session, project) == 1

    item = session.scalar(select(MediaItem))
    assert item.canonical_url == canonicalize("https://www.youtube.com/watch?v=abc123")
    assert item.search_source == "duckduckgo_video"
    assert item.source_name == "ISP RJ"
    assert item.view_count == 1_868
    assert item.source_provenance[0]["view_count"] == 1_868
    assert item.source_provenance[0]["source"] == "duckduckgo_video"

    services._record_source_provenance(
        item,
        source="tavily",
        title="Vídeo encontrado",
        url=item.url,
        published_at="2026-08-12",
        snippet="Descrição do vídeo",
    )
    session.commit()
    monkeypatch.setattr(
        services,
        "structured_response",
        lambda **_kwargs: {
            "status": "PARTIALLY_CONFIRMED",
            "matching_fields": ["url", "title", "published_at"],
            "conflicting_fields": [],
            "detail": "URL, título e data coincidem; Tavily não informou o canal.",
        },
    )
    result = services.validate_tavily_youtube_metadata(session, project)
    assert result["validated"] == 1
    assert result["skipped"] is False
    assert item.cross_validation_status == "PARTIALLY_CONFIRMED"
    session.close()


def test_collect_youtube_tavily_accepts_only_youtube_urls(monkeypatch):
    from app import services
    from app.config import Settings

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={"actors": [], "actions": [], "locations": []},
    )
    session.add(project)
    session.commit()

    class FakeTavilyClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def search(self, **_kwargs):
            return {
                "results": [
                    {
                        "title": "Vídeo Tavily",
                        "url": "https://www.youtube.com/watch?v=tav123",
                        "content": "Descrição do vídeo",
                        "published_date": "2026-08-20",
                        "source": "YouTube",
                    },
                    {
                        "title": "Resultado externo",
                        "url": "https://example.com/video",
                        "content": "externo",
                        "published_date": "2026-08-20",
                        "source": "Blog",
                    },
                ]
            }

    import tavily

    monkeypatch.setattr(tavily, "TavilyClient", FakeTavilyClient)
    monkeypatch.setattr(
        services, "get_settings", lambda: Settings(tavily_api_key="teste")
    )
    assert services.collect_youtube_tavily(session, project) == 1

    item = session.scalar(select(MediaItem))
    assert item.canonical_url == canonicalize("https://www.youtube.com/watch?v=tav123")
    assert item.search_source == "tavily"
    assert item.source_provenance[0]["source"] == "tavily"
    session.close()


def test_youtube_metrics_list_all_priority_channels_and_exclude_conflicts():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema de teste",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
    )
    session.add(project)
    session.flush()
    session.add_all(
        [
            MediaItem(
                project_id=project.id, title="Vídeo ISP", url="https://www.youtube.com/watch?v=isp",
                canonical_url="https://www.youtube.com/watch?v=isp", domain="www.youtube.com",
                source_name="ISP RJ", view_count=10, status="VALID",
            ),
            MediaItem(
                project_id=project.id, title="Vídeo conflituoso", url="https://www.youtube.com/watch?v=conflict",
                canonical_url="https://www.youtube.com/watch?v=conflict", domain="www.youtube.com",
                source_name="CNN Brasil", view_count=100, status="VALID", cross_validation_status="CONFLICT",
            ),
        ]
    )
    session.commit()

    result = metrics(session, project.id)
    checks = result["youtube_priority_channel_checks"]
    assert len(checks) == len(PRIORITY_YOUTUBE_CHANNELS)
    assert next(row for row in checks if row["channel"] == "ISP RJ")["result"] == "com cobertura auditável"
    assert next(row for row in checks if row["channel"] == "CNN Brasil")["result"] == "sem item validado na amostra"
    assert result["youtube_conflicts_excluded"] == 1
    assert all(row["channel"] != "CNN Brasil" for row in result["top_youtube_channels"])
    session.close()

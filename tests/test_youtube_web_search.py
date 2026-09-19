from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project
from app.services import canonicalize, is_youtube_url, metrics
from app.services.collection.common import record_source_provenance
from app.services.collection.persist import persist_video_rows
from app.services import validation as services_validation
from app.source_registry import PRIORITY_YOUTUBE_CHANNELS
from app.tools import search as tools_search


def test_youtube_url_validation_accepts_only_youtube_hosts():
    assert is_youtube_url("https://www.youtube.com/watch?v=abc123")
    assert is_youtube_url("https://youtu.be/abc123")
    assert not is_youtube_url("https://youtube.com.evil.example/watch?v=abc123")
    assert not is_youtube_url("https://example.com/watch?v=abc123")
    assert canonicalize("https://youtu.be/abc123") == canonicalize("https://www.youtube.com/watch?v=abc123")


def test_video_persistence_rejects_external_urls_and_records_provenance(monkeypatch):
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

    task = SimpleNamespace(target="Busca temática", query="tema de teste video", is_priority=False)
    rows = [
        {
            "title": "Vídeo encontrado",
            "url": "https://www.youtube.com/watch?v=abc123&utm_source=test",
            "snippet": "Descrição do vídeo",
            "content": "Descrição do vídeo",
            "published_at": "2026-08-12",
            "source_name": "ISP RJ",
            "view_count": 1_868,
            "provider": "duckduckgo_video",
        },
        {
            "title": "Resultado externo",
            "url": "https://example.com/video",
            "snippet": None,
            "content": None,
            "published_at": None,
            "source_name": None,
            "view_count": None,
            "provider": "duckduckgo_video",
        },
    ]

    counters, accepted = persist_video_rows(
        session,
        project,
        task,
        {},
        rows,
        has_window=True,
        start=date(2026, 8, 1),
        end=date(2026, 8, 31),
    )
    session.commit()

    assert counters["added"] == 1
    assert counters["rejected"] == 1
    assert len(accepted) == 1

    item = session.scalar(select(MediaItem))
    assert item.canonical_url == canonicalize("https://www.youtube.com/watch?v=abc123")
    assert item.search_source == "duckduckgo_video"
    assert item.source_name == "ISP RJ"
    assert item.view_count == 1_868
    assert item.source_provenance[0]["view_count"] == 1_868
    assert item.source_provenance[0]["source"] == "duckduckgo_video"

    record_source_provenance(
        item,
        source="duckduckgo_video",
        title="Vídeo encontrado",
        url=item.url,
        published_at="2026-08-12",
        snippet="Descrição do vídeo",
    )
    session.commit()
    result = services_validation.validate_video_metadata_cross_source(session, project)
    assert result["validated"] == 0
    assert result["skipped"] is True
    session.close()


def test_duckduckgo_video_search_returns_only_provider_results(monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        tools_search, "get_settings", lambda: Settings()
    )
    monkeypatch.setattr(
        tools_search,
        "duckduckgo_videos",
        lambda *args, **kwargs: [
            {
                "title": "Vídeo DDG",
                "url": "https://www.youtube.com/watch?v=ddg123",
                "description": "Descrição do vídeo",
                "published_at": "2026-08-20",
                "channel": "YouTube",
                "view_count": 10,
                "provider": "duckduckgo_video",
            }
        ],
    )

    provider, rows = tools_search._search_videos("consulta", providers=("duckduckgo",))

    assert provider == "duckduckgo"
    assert [row["url"] for row in rows] == ["https://www.youtube.com/watch?v=ddg123"]
    assert rows[0]["provider"] == "duckduckgo_video"


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

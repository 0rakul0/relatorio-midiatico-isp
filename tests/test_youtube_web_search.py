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


def test_youtube_result_is_derived_from_primary_web_discovery():
    from app.services.collection.youtube import _youtube_result

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
                project_id=project.id,
                title="Video descoberto na busca principal",
                url="https://www.youtube.com/watch?v=web123",
                canonical_url="https://www.youtube.com/watch?v=web123",
                domain="youtube.com",
                status="PENDING",
                media_origin="YOUTUBE",
                search_source="duckduckgo",
            ),
            MediaItem(
                project_id=project.id,
                title="Materia tradicional",
                url="https://example.com/materia",
                canonical_url="https://example.com/materia",
                domain="example.com",
                status="PENDING",
                media_origin="PORTAL_NOTICIAS",
                search_source="duckduckgo",
            ),
        ]
    )
    session.commit()

    result = _youtube_result(session, project.id)

    assert result["status"] == "ROUTED"
    assert result["provider"] == "duckduckgo"
    assert result["collected"] == 1
    assert result["stats"]["routed_from_web"] == 1
    session.close()

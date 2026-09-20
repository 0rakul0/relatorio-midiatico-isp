from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project
from app.services.metrics import metrics


def test_top_youtube_channels_never_leak_negative_sentinels():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="tema",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        topic_profile={},
    )
    session.add(project)
    session.commit()
    session.add(
        MediaItem(
            project_id=project.id,
            title="Video sem metricas",
            url="https://www.youtube.com/watch?v=abc123",
            canonical_url="https://www.youtube.com/watch?v=abc123",
            domain="www.youtube.com",
            source_name="Canal X",
            view_count=None,
            status="VALID",
            fact_status="PENDING",
        )
    )
    session.commit()

    data = metrics(session, project.id)
    assert data["youtube_videos"] == 1
    top = data["top_youtube_channels"][0]
    assert top["channel"] == "Canal X"
    assert top["views"] is None
    assert top["lead_views"] is None
    assert "view_count_items" not in top
    session.close()

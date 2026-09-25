from datetime import date
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import MediaItem, Project, SocialComment, SocialPost
from app.services import social_repercussion as social


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_social_collection_persists_comments_without_author_identity(monkeypatch):
    db = _session()
    project = Project(
        topic="tema social",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={},
        execution_options={},
        execution_plan={},
    )
    db.add(project)
    db.flush()
    db.add(
        MediaItem(
            project_id=project.id,
            title="Post Instagram",
            url="https://www.instagram.com/p/ABC123/",
            canonical_url="https://www.instagram.com/p/ABC123/",
            domain="instagram.com",
            status="VALID",
            media_origin="REDE_SOCIAL",
            search_source="duckduckgo",
        )
    )
    db.commit()

    monkeypatch.setattr(
        social,
        "get_settings",
        lambda: SimpleNamespace(
            apify_social_enabled=True,
            apify_api_token="token",
            apify_instagram_comments_actor_id="apify/instagram-comment-scraper",
            apify_facebook_comments_actor_id="apify/facebook-comments-scraper",
            apify_x_comments_actor_id=None,
            apify_social_max_posts_per_platform=8,
            apify_social_comments_per_post=50,
            apify_api_base_url="https://api.apify.com/v2",
            apify_social_timeout_seconds=240,
            social_analysis_max_comments=120,
            social_analysis_batch_size=30,
        ),
    )
    monkeypatch.setattr(
        social,
        "collect_public_comments",
        lambda **_kwargs: (
            "apify/instagram-comment-scraper",
            [
                {
                    "id": "c1",
                    "text": "Tenho medo de sair a noite.",
                    "postUrl": "https://www.instagram.com/p/ABC123/",
                    "timestamp": "2026-08-10T20:00:00Z",
                    "likesCount": 4,
                    "ownerUsername": "nao-deve-ser-persistido",
                }
            ],
        ),
    )
    monkeypatch.setattr(social, "llm_is_configured", lambda: False)

    result = social.collect_social_repercussion(db, project)

    assert result["comments"] == 1
    assert result["status"] == "COLLECTED_ONLY"
    assert db.scalar(select(SocialPost)) is not None
    comment = db.scalar(select(SocialComment))
    assert comment is not None
    assert comment.text == "Tenho medo de sair a noite."
    assert not hasattr(comment, "author_name")
    assert not hasattr(comment, "username")
    db.close()


def test_platform_detection_only_accepts_post_urls():
    assert social._platform_for_url(
        "https://www.instagram.com/p/ABC/"
    ) == "instagram"
    assert social._platform_for_url(
        "https://www.facebook.com/page/posts/123"
    ) == "facebook"
    assert social._platform_for_url(
        "https://x.com/user/status/123"
    ) == "x"
    assert social._platform_for_url(
        "https://www.instagram.com/perfil/"
    ) is None
    assert social._platform_for_url(
        "https://www.facebook.com/perfil/"
    ) is None

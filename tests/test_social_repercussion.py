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
            social_discovery_enabled=False,
            social_discovery_queries_per_platform=2,
            social_discovery_results_per_query=8,
            apify_instagram_comments_actor_id="apify/instagram-comment-scraper",
            apify_facebook_comments_actor_id="apify/facebook-comments-scraper",
            apify_tiktok_comments_actor_id="clockworks/tiktok-comments-scraper",
            apify_x_comments_actor_id=None,
            apify_social_max_posts_per_platform=8,
            apify_social_comments_per_post=50,
            apify_api_base_url="https://api.apify.com/v2",
            apify_social_timeout_seconds=240,
            social_analysis_max_comments=120,
            social_analysis_batch_size=30,
            social_reuse_max_age_days=30,
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
        "https://www.tiktok.com/@usuario/video/7332342275151760642"
    ) == "tiktok"
    assert social._platform_for_url(
        "https://x.com/user/status/123"
    ) == "x"
    assert social._platform_for_url(
        "https://www.instagram.com/perfil/"
    ) is None
    assert social._platform_for_url(
        "https://www.facebook.com/perfil/"
    ) is None


def test_x_reply_shape_is_normalized():
    row = {
        "replyId": "1906884256397189461",
        "replyText": "A seguranca precisa melhorar.",
        "postUrl": "https://x.com/exemplo/status/1906833084554650018",
        "timestamp": 1743471609000,
        "favouriteCount": 7,
        "replyCount": 2,
        "author": {"screenName": "nao-persistir"},
    }

    normalized = social._normalize_comment("x", row)

    assert normalized is not None
    assert normalized["external_id"] == "1906884256397189461"
    assert normalized["text"] == "A seguranca precisa melhorar."
    assert normalized["like_count"] == 7
    assert normalized["reply_count"] == 2
    assert normalized["published_at"] is not None
    assert "author" not in normalized


def test_tiktok_comment_shape_is_normalized():
    row = {
        "cid": "7500000000000000001",
        "text": "Esse tema precisa de mais atencao.",
        "createTimeISO": "2026-08-06T11:00:00.000Z",
        "diggCount": 42,
        "replyCommentTotal": 3,
        "videoWebUrl": "https://www.tiktok.com/@exemplo/video/7332342275151760642",
        "uniqueId": "nao-persistir",
    }

    normalized = social._normalize_comment("tiktok", row)

    assert normalized is not None
    assert normalized["external_id"] == "7500000000000000001"
    assert normalized["text"] == "Esse tema precisa de mais atencao."
    assert normalized["like_count"] == 42
    assert normalized["reply_count"] == 3
    assert normalized["source_url"].endswith("/7332342275151760642")
    assert normalized["published_at"] is not None


def test_social_candidates_accept_duckduckgo_and_reused_provenance():
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
    db.add_all(
        [
            MediaItem(
                project_id=project.id,
                title="Post descoberto",
                url="https://www.instagram.com/p/DDG123/",
                canonical_url="https://www.instagram.com/p/DDG123/",
                domain="instagram.com",
                status="PENDING",
                media_origin="REDE_SOCIAL",
                search_source="duckduckgo_news",
            ),
            MediaItem(
                project_id=project.id,
                title="Post reutilizado",
                url="https://www.instagram.com/p/REUSED123/",
                canonical_url="https://www.instagram.com/p/REUSED123/",
                domain="instagram.com",
                status="PENDING",
                media_origin="REDE_SOCIAL",
                search_source="corpus_reuse",
                source_provenance=[{"provider": "duckduckgo", "origin": "historical"}],
            ),
            MediaItem(
                project_id=project.id,
                title="Post manual",
                url="https://www.instagram.com/p/MANUAL123/",
                canonical_url="https://www.instagram.com/p/MANUAL123/",
                domain="instagram.com",
                status="PENDING",
                media_origin="REDE_SOCIAL",
                search_source="manual",
            ),
        ]
    )
    db.commit()

    candidates = social._candidate_urls(db, project.id)

    assert "instagram" in candidates
    urls = [row[0] for row in candidates["instagram"]]
    assert "https://www.instagram.com/p/DDG123/" in urls
    assert "https://www.instagram.com/p/REUSED123/" in urls
    assert "https://www.instagram.com/p/MANUAL123/" not in urls
    db.close()



def test_social_discovery_persists_direct_post_urls(monkeypatch):
    db = _session()
    project = Project(
        topic="polarização política no Brasil em 2026",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={
            "search_synonyms": [
                "polarização política Brasil 2026",
                "Lula Bolsonaro polarização",
            ]
        },
        execution_options={},
        execution_plan={},
    )
    db.add(project)
    db.commit()

    monkeypatch.setattr(
        social,
        "get_settings",
        lambda: SimpleNamespace(
            social_discovery_enabled=True,
            social_discovery_queries_per_platform=2,
            social_discovery_results_per_query=8,
            apify_social_max_posts_per_platform=8,
        ),
    )
    monkeypatch.setattr(
        social,
        "discover_public_posts",
        lambda **_kwargs: {
            "instagram": [{
                "title": "Debate sobre polarização",
                "url": "https://www.instagram.com/p/POLAR123/",
                "snippet": "Discussão sobre polarização política.",
                "provider": "duckduckgo_social",
                "discovery_query": 'site:instagram.com "polarização política Brasil 2026"',
            }],
            "facebook": [],
            "tiktok": [],
            "x": [{
                "title": "Polarização e eleições",
                "url": "https://x.com/exemplo/status/123456",
                "snippet": "Debate eleitoral.",
                "provider": "duckduckgo_social",
                "discovery_query": 'site:x.com "Lula Bolsonaro polarização"',
            }],
        },
    )

    stats = social._discover_and_persist_social_posts(db, project)

    assert stats["eligible_posts"] == 2
    assert stats["new_items"] == 2
    rows = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    assert len(rows) == 2
    assert all(row.media_origin == "REDE_SOCIAL" for row in rows)
    assert all(row.search_source == "duckduckgo_social" for row in rows)
    db.close()



def test_social_report_exposes_post_inventory_with_comment_counts():
    db = _session()
    project = Project(
        topic="tema social auditável",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={},
        execution_options={},
        execution_plan={},
    )
    db.add(project)
    db.flush()
    item = MediaItem(
        project_id=project.id,
        title="Post sobre o tema",
        url="https://x.com/exemplo/status/777",
        canonical_url="https://x.com/exemplo/status/777",
        domain="x.com",
        status="PENDING",
        media_origin="REDE_SOCIAL",
        search_source="duckduckgo_social",
    )
    db.add(item)
    db.flush()
    post = SocialPost(
        project_id=project.id,
        media_item_id=item.id,
        platform="x",
        url=item.url,
        post_text=item.title,
        actor_id="actor/test",
    )
    db.add(post)
    db.flush()
    db.add_all([
        SocialComment(
            project_id=project.id,
            social_post_id=post.id,
            platform="x",
            external_id="c1",
            text="comentário 1",
            source_url=post.url,
        ),
        SocialComment(
            project_id=project.id,
            social_post_id=post.id,
            platform="x",
            external_id="c2",
            text="comentário 2",
            source_url=post.url,
        ),
    ])
    db.commit()

    report = social.social_repercussion_for_report(db, project.id)

    assert report["posts"] == 1
    assert report["comments"] == 2
    assert len(report["post_inventory"]) == 1
    row = report["post_inventory"][0]
    assert row["platform"] == "x"
    assert row["title"] == "Post sobre o tema"
    assert row["comments_collected"] == 2
    assert row["discovery_source"] == "duckduckgo_social"
    assert row["collector"] == "Apify"
    db.close()

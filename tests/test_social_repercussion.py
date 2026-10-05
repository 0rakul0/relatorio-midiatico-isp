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



def test_social_analysis_generates_discursive_reading(monkeypatch):
    db = _session()
    project = Project(
        topic="polarização política",
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
    post = SocialPost(
        project_id=project.id,
        platform="x",
        url="https://x.com/exemplo/status/999",
        post_text="debate",
    )
    db.add(post)
    db.flush()
    db.add_all([
        SocialComment(
            project_id=project.id, social_post_id=post.id, platform="x",
            external_id="d1", text="Só sabem atacar o outro lado", source_url=post.url,
        ),
        SocialComment(
            project_id=project.id, social_post_id=post.id, platform="x",
            external_id="d2", text="Estou cansado dessa briga política", source_url=post.url,
        ),
    ])
    db.commit()

    class FakeAgent:
        calls = 0
        def run(self, **kwargs):
            self.calls += 1
            if kwargs["task"] == "social_comment_analysis":
                return {
                    "assessments": [
                        {"index": 0, "sentiment": "NEGATIVO", "emotion": "INDIGNACAO", "position": "CRITICA", "themes": ["confronto"]},
                        {"index": 1, "sentiment": "NEGATIVO", "emotion": "DESCONFIANCA", "position": "CRITICA", "themes": ["fadiga"]},
                    ]
                }
            assert kwargs["task"] == "social_discourse_analysis"
            return {
                "overall_reading": "Entre os comentários analisados, há rejeição ao confronto e sinais de fadiga com a disputa.",
                "dominant_narratives": [{"title": "Fadiga", "analysis": "Parte da amostra critica a continuidade do conflito político."}],
                "recurring_arguments": [],
                "tensions_and_contradictions": [],
                "interaction_patterns": [{"title": "Confronto", "analysis": "O outro campo aparece como alvo recorrente de crítica."}],
                "polarization_signals": "Há sinais de polarização afetiva na forma de rejeição ao campo adversário, sem inferência populacional.",
                "sample_limitations": "A amostra é composta apenas por comentários públicos recuperados dos posts monitorados.",
            }

    fake = FakeAgent()
    monkeypatch.setattr(social, "llm_is_configured", lambda: True)
    monkeypatch.setattr(social, "get_report_agent", lambda: fake)
    monkeypatch.setattr(
        social,
        "get_settings",
        lambda: SimpleNamespace(
            social_analysis_max_comments=120,
            social_analysis_batch_size=30,
        ),
    )

    report = social.analyze_social_comments(db, project)

    assert report["analyzed_comments"] == 2
    assert "rejeição ao confronto" in report["summary"]
    assert report["discourse_analysis"]["dominant_narratives"][0]["title"] == "Fadiga"
    assert fake.calls == 2
    db.close()



def test_report_agent_knows_social_discourse_analysis_task():
    from app.agent import ReportAgent

    prompt = ReportAgent().prompt_for("social_discourse_analysis")

    assert "ANALISE DISCURSIVA" in prompt
    assert "nao generalize" in prompt.lower()



def test_social_terms_prioritize_compact_profile_variants_over_literal_question():
    project = Project(
        topic="como está a polarização política e como ficou a eleição no Brasil em 2026?",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={
            "subject_terms": ["polarização política", "eleições 2026"],
            "actors": ["Lula", "Bolsonaro"],
            "search_synonyms": ["polarização eleitoral Brasil"],
        },
        execution_options={},
        execution_plan={},
    )

    terms = social._social_discovery_terms(project)

    assert terms[0] == "polarização eleitoral Brasil"
    assert "polarização política" in terms[:4]
    assert "eleições 2026" in terms[:4]
    assert "Lula Bolsonaro" in terms


def test_social_discovery_runs_broader_second_round_when_first_is_empty(monkeypatch):
    db = _session()
    project = Project(
        topic="como está a polarização política no Brasil em 2026?",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={
            "subject_terms": ["polarização política"],
            "actors": ["Lula", "Bolsonaro"],
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

    calls = []
    def fake_discover(**kwargs):
        calls.append(list(kwargs["terms"]))
        if len(calls) == 1:
            return {"instagram": [], "facebook": [], "tiktok": [], "x": []}
        return {
            "instagram": [],
            "facebook": [],
            "tiktok": [],
            "x": [{
                "title": "Debate eleitoral",
                "url": "https://x.com/exemplo/status/2026",
                "snippet": "Polarização e eleição",
                "discovery_query": "site:x.com polarização política Brasil 2026",
            }],
        }

    monkeypatch.setattr(social, "discover_public_posts", fake_discover)

    stats = social._discover_and_persist_social_posts(db, project)

    assert len(calls) == 2
    assert stats["rounds"] == 2
    assert stats["returned"] == 1
    assert stats["eligible_posts"] == 1
    assert stats["platforms"]["x"]["returned"] == 1
    db.close()



def test_x_post_date_can_be_derived_from_snowflake_url():
    published_at, source = social._post_datetime_from_url(
        "x",
        "https://x.com/jack/status/20",
    )
    # ID 20 é anterior à era útil do Snowflake do X e deve ser descartado.
    assert published_at is None
    assert source is None


def test_tiktok_post_date_can_be_derived_from_video_id():
    timestamp = 1_700_000_000
    video_id = timestamp << 32
    published_at, source = social._post_datetime_from_url(
        "tiktok",
        f"https://www.tiktok.com/@usuario/video/{video_id}",
    )
    assert published_at is not None
    assert published_at.year == 2023
    assert source == "tiktok_video_id"


def test_post_date_prefers_apify_post_metadata_over_comment_timestamp():
    row = {
        "postUrl": "https://www.instagram.com/p/ABC123/",
        "timestamp": "2026-10-05T15:00:00Z",
        "postCreatedAt": "2026-09-29T10:30:00Z",
    }

    published_at, source = social._post_datetime_from_row("instagram", row)

    assert published_at is not None
    assert published_at.date() == date(2026, 9, 29)
    assert source == "apify_post_metadata"


def test_social_collection_promotes_apify_post_date_to_social_post(monkeypatch):
    db = _session()
    project = Project(
        topic="tema social com data",
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
        title="Post Instagram",
        url="https://www.instagram.com/p/DATA123/",
        canonical_url="https://www.instagram.com/p/DATA123/",
        domain="instagram.com",
        status="PENDING",
        media_origin="REDE_SOCIAL",
        search_source="duckduckgo_social",
    )
    db.add(item)
    db.commit()

    monkeypatch.setattr(
        social,
        "get_settings",
        lambda: SimpleNamespace(
            apify_social_enabled=True,
            apify_api_token="token",
            social_discovery_enabled=False,
            social_discovery_queries_per_platform=4,
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
            [{
                "id": "c-date",
                "text": "comentário",
                "postUrl": item.url,
                "timestamp": "2026-10-02T12:00:00Z",
                "postCreatedAt": "2026-09-28T09:00:00Z",
            }],
        ),
    )
    monkeypatch.setattr(social, "llm_is_configured", lambda: False)

    social.collect_social_repercussion(db, project)

    post = db.scalar(select(SocialPost).where(SocialPost.project_id == project.id))
    refreshed_item = db.get(MediaItem, item.id)
    assert post is not None
    assert post.published_at is not None
    assert post.published_at.date() == date(2026, 9, 28)
    assert refreshed_item.published_at == date(2026, 9, 28)
    assert any(
        entry.get("source") == "social_post_date"
        and entry.get("date_source") == "apify_post_metadata"
        for entry in (refreshed_item.source_provenance or [])
        if isinstance(entry, dict)
    )
    db.close()



def test_duckduckgo_relative_days_use_retrieval_time():
    row = {
        "published_at": "3 days ago",
        "published_at_raw": "3 days ago",
        "retrieved_at": "2026-10-05T10:30:00+00:00",
    }

    published_at, source, raw = social._duckduckgo_result_date(row)

    assert published_at == date(2026, 10, 2)
    assert source == "duckduckgo_relative_date"
    assert raw == "3 days ago"


def test_duckduckgo_relative_date_supports_portuguese():
    row = {
        "published_at": "há 2 dias",
        "retrieved_at": "2026-10-05T10:30:00+00:00",
    }

    published_at, source, raw = social._duckduckgo_result_date(row)

    assert published_at == date(2026, 10, 3)
    assert source == "duckduckgo_relative_date"
    assert raw == "há 2 dias"


def test_duckduckgo_relative_hours_can_cross_calendar_day():
    row = {
        "published_at": "5 hours ago",
        "retrieved_at": "2026-10-05T02:00:00+00:00",
    }

    published_at, source, _raw = social._duckduckgo_result_date(row)

    assert published_at == date(2026, 10, 4)
    assert source == "duckduckgo_relative_date"


def test_social_discovery_persists_relative_duckduckgo_date(monkeypatch):
    db = _session()
    project = Project(
        topic="eleições 2026",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 10, 5),
        has_custom_date_window=True,
        project_type="GENERAL_TOPIC",
        topic_profile={"subject_terms": ["eleições 2026"]},
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
            social_discovery_queries_per_platform=1,
            social_discovery_results_per_query=8,
            apify_social_max_posts_per_platform=8,
        ),
    )
    monkeypatch.setattr(
        social,
        "discover_public_posts",
        lambda **_kwargs: {
            "instagram": [{
                "title": "Eleições 2026",
                "url": "https://www.instagram.com/p/RELDATE/",
                "snippet": "Debate eleitoral",
                "published_at": "3 days ago",
                "published_at_raw": "3 days ago",
                "retrieved_at": "2026-10-05T10:30:00+00:00",
                "discovery_query": "site:instagram.com eleições 2026",
            }],
            "facebook": [],
            "tiktok": [],
            "x": [],
        },
    )

    social._discover_and_persist_social_posts(db, project)

    item = db.scalar(select(MediaItem).where(MediaItem.project_id == project.id))
    assert item is not None
    assert item.published_at == date(2026, 10, 2)
    assert any(
        entry.get("date_source") == "duckduckgo_relative_date"
        and entry.get("published_at_raw") == "3 days ago"
        for entry in (item.source_provenance or [])
        if isinstance(entry, dict)
    )
    db.close()



def test_social_sample_balances_platforms_and_posts():
    from datetime import datetime

    comments = []
    next_id = 1
    # Um post do Facebook muito volumoso não deve dominar os demais.
    for idx in range(12):
        comments.append(SimpleNamespace(
            id=next_id,
            platform="facebook",
            social_post_id=1,
            like_count=100 - idx,
            reply_count=idx % 3,
            published_at=datetime(2026, 10, 1, 12, idx),
        ))
        next_id += 1
    for post_id in (2, 3):
        comments.append(SimpleNamespace(
            id=next_id,
            platform="facebook",
            social_post_id=post_id,
            like_count=1,
            reply_count=0,
            published_at=datetime(2026, 10, 2, 12, post_id),
        ))
        next_id += 1
    for post_id in (10, 11):
        comments.append(SimpleNamespace(
            id=next_id,
            platform="instagram",
            social_post_id=post_id,
            like_count=2,
            reply_count=1,
            published_at=datetime(2026, 10, 3, 12, post_id - 10),
        ))
        next_id += 1

    sample = social._balanced_sample(comments, 8)

    assert len(sample) == 8
    assert {row.platform for row in sample} == {"facebook", "instagram"}
    assert {row.social_post_id for row in sample if row.platform == "facebook"} >= {1, 2, 3}
    assert {row.social_post_id for row in sample if row.platform == "instagram"} == {10, 11}


def test_social_sample_mixes_high_and_low_engagement_within_post():
    from datetime import datetime

    comments = [
        SimpleNamespace(
            id=1, platform="x", social_post_id=99,
            like_count=100, reply_count=1,
            published_at=datetime(2026, 10, 1, 10, 0),
        ),
        SimpleNamespace(
            id=2, platform="x", social_post_id=99,
            like_count=5, reply_count=30,
            published_at=datetime(2026, 10, 2, 10, 0),
        ),
        SimpleNamespace(
            id=3, platform="x", social_post_id=99,
            like_count=0, reply_count=0,
            published_at=datetime(2026, 10, 3, 10, 0),
        ),
        SimpleNamespace(
            id=4, platform="x", social_post_id=99,
            like_count=10, reply_count=2,
            published_at=datetime(2026, 10, 5, 10, 0),
        ),
    ]

    ordered = social._mixed_post_comment_order(comments)

    first_ids = {row.id for row in ordered[:4]}
    assert first_ids == {1, 2, 3, 4}


def test_discourse_analysis_uses_all_classified_comments(monkeypatch):
    db = _session()
    project = Project(
        topic="tema social amplo",
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
    post = SocialPost(
        project_id=project.id,
        platform="x",
        url="https://x.com/exemplo/status/120",
        post_text="debate",
    )
    db.add(post)
    db.flush()
    for idx in range(120):
        db.add(SocialComment(
            project_id=project.id,
            social_post_id=post.id,
            platform="x",
            external_id=f"all-{idx}",
            text=f"comentário {idx}",
            like_count=idx,
            reply_count=idx % 5,
            source_url=post.url,
        ))
    db.commit()

    discourse_sizes = []
    class FakeAgent:
        def run(self, **kwargs):
            if kwargs["task"] == "social_comment_analysis":
                return {
                    "assessments": [
                        {
                            "index": row["index"],
                            "sentiment": "NEUTRO",
                            "emotion": "NAO_IDENTIFICAVEL",
                            "position": "OUTRA",
                            "themes": [],
                        }
                        for row in kwargs["payload"]["comments"]
                    ]
                }
            discourse_sizes.append(len(kwargs["payload"]["comments"]))
            return {
                "overall_reading": "Entre os comentários analisados, há diversidade de posições sem generalização populacional.",
                "dominant_narratives": [],
                "recurring_arguments": [],
                "tensions_and_contradictions": [],
                "interaction_patterns": [],
                "polarization_signals": "A amostra não sustenta inferências além dos comentários observados.",
                "sample_limitations": "Comentários públicos monitorados não representam a população.",
            }

    monkeypatch.setattr(social, "llm_is_configured", lambda: True)
    monkeypatch.setattr(social, "get_report_agent", lambda: FakeAgent())
    monkeypatch.setattr(
        social,
        "get_settings",
        lambda: SimpleNamespace(
            social_analysis_max_comments=120,
            social_analysis_batch_size=30,
        ),
    )

    report = social.analyze_social_comments(db, project)

    assert report["analyzed_comments"] == 120
    assert discourse_sizes == [120]
    db.close()

def test_social_view_metric_accepts_common_apify_fields():
    from app.social.engagement import int_metric, post_view_count

    assert int_metric("12.5k") == 12500
    assert int_metric("1,2m") == 1200000
    assert post_view_count({"playCount": 3210}) == (3210, "playCount")
    assert post_view_count({"statistics": {"viewCount": "4500"}}) == (
        4500,
        "statistics.viewCount",
    )


def test_social_report_aggregates_post_and_media_views():
    from app.social.reporting import social_repercussion_for_report

    db = _session()
    project = Project(
        topic="tema com alcance social",
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

    media = MediaItem(
        project_id=project.id,
        title="Post com views herdadas",
        url="https://www.instagram.com/p/VIEWS1/",
        canonical_url="https://www.instagram.com/p/VIEWS1/",
        domain="instagram.com",
        media_origin="REDE_SOCIAL",
        search_source="duckduckgo_social",
        view_count=1500,
    )
    db.add(media)
    db.flush()

    db.add_all([
        SocialPost(
            project_id=project.id,
            media_item_id=media.id,
            platform="instagram",
            url=media.url,
            post_text="post 1",
        ),
        SocialPost(
            project_id=project.id,
            platform="tiktok",
            url="https://www.tiktok.com/@u/video/123",
            post_text="post 2",
            view_count=2500,
            view_count_source="apify:playCount",
        ),
    ])
    db.commit()

    report = social_repercussion_for_report(db, project.id)

    assert report["view_count_total"] == 4000
    assert report["view_count_known_posts"] == 2
    assert report["view_count_missing_posts"] == 0
    assert report["platform_view_counts"]["instagram"] == 1500
    assert report["platform_view_counts"]["tiktok"] == 2500
    assert report["post_inventory"][0]["view_count"] in {1500, 2500}
    db.close()


def test_apify_post_metadata_updates_social_post_views():
    from app.social.persistence import _enrich_posts_from_apify_dataset

    db = _session()
    project = Project(
        topic="tema social",
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
    post = SocialPost(
        project_id=project.id,
        platform="tiktok",
        url="https://www.tiktok.com/@u/video/123456789",
        post_text="vídeo",
    )
    db.add(post)
    db.flush()

    updated = _enrich_posts_from_apify_dataset(
        db,
        platform="tiktok",
        dataset=[{
            "postUrl": post.url,
            "postCreatedAt": "2026-10-01T10:00:00Z",
            "playCount": 98765,
        }],
        post_by_url={social._url_key(post.url): post},
    )
    db.flush()

    assert updated == 1
    assert post.view_count == 98765
    assert post.view_count_source == "apify:playCount"
    db.close()


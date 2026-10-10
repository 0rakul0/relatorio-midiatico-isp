from types import SimpleNamespace

from app.social.discovery import social_topic_relevance, social_discovery_terms


def project():
    return SimpleNamespace(
        topic="violencia contra mulheres no Brasil",
        topic_profile={"actors": ["Lula", "Bolsonaro"], "subject_terms": ["feminicidio"]},
        collection_end=None,
    )


def test_electoral_posts_not_eligible():
    eligible, reason = social_topic_relevance(project(), "Eleições presidenciais Brasil 2026")
    assert not eligible
    assert reason == "ELECTORAL_CONTENT_WITHOUT_GENDER_VIOLENCE_LINK"


def test_gender_political_violence_remains_eligible():
    eligible, _ = social_topic_relevance(project(), "Violencia politica contra mulheres cresce")
    assert eligible


def test_social_queries_do_not_use_isolated_political_actors():
    terms = social_discovery_terms(project())
    assert "Lula" not in terms
    assert "Bolsonaro" not in terms
    assert project().topic in terms


def test_gender_violence_not_diluted_by_generic_elections():
    assert not social_topic_relevance(project(), "Pesquisa Quaest: corrida presidencial")[0]
    assert social_topic_relevance(project(), "Medidas protetivas para vitimas de violencia domestica")[0]
    assert social_topic_relevance(project(), "Violencia politica contra mulheres nas eleicoes")[0]


def test_off_topic_social_post_excluded_from_reporting():
    from datetime import date
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.models import Project, SocialPost
    from app.social.reporting import social_repercussion_for_report

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        record = Project(
            topic="violencia contra mulheres no Brasil",
            launch_date=date(2026, 1, 1),
            collection_start=date(2026, 1, 1),
            collection_end=date(2026, 12, 31),
            project_type="GENERAL_TOPIC",
            topic_profile={},
            execution_options={},
        )
        db.add(record)
        db.flush()
        db.add_all([
            SocialPost(project_id=record.id, platform="facebook", url="https://facebook.com/a/posts/1", post_text="Eleições presidenciais 2026"),
            SocialPost(project_id=record.id, platform="facebook", url="https://facebook.com/a/posts/2", post_text="Violencia domestica contra mulheres"),
        ])
        db.commit()
        data = social_repercussion_for_report(db, record.id)
        assert data["posts"] == 1
        assert data["excluded_posts"] == 1
        assert "violencia domestica" in data["post_inventory"][0]["title"].lower()

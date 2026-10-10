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

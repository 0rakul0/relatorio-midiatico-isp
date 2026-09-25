from datetime import date

from app.topic_profile import heuristic_topic_profile, requested_month_window


def test_requested_month_window():
    start, end = requested_month_window("policiais mortos em agosto de 2026 no Rio de Janeiro")
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)


def test_event_topic_does_not_depend_on_product_launch():
    profile = heuristic_topic_profile("policiais mortos em agosto de 2026 no Rio de Janeiro")
    assert profile["project_type"] == "EVENT_TOPIC"
    assert profile["event_type"] == "DEATH"
    assert "policial" in profile["actors"]


def test_known_event_prevails_over_generic_product_term():
    # "relatório" é termo genérico de produto e não pode transformar um fato
    # noticiado em produto institucional.
    profile = heuristic_topic_profile("relatório sobre mortes por intervenção policial em 2026")
    assert profile["project_type"] == "EVENT_TOPIC"
    assert profile["event_type"] == "DEATH_BY_STATE_INTERVENTION"


def test_generic_product_term_alone_stays_institutional_product():
    profile = heuristic_topic_profile("Dossiê Mulher 2026")
    assert profile["project_type"] == "INSTITUTIONAL_PRODUCT"
    assert profile["product_anchor"] == "Dossiê Mulher"


def test_known_location_typo_is_canonicalized_without_losing_original_variant():
    topic = "produção habitacional milícia mazuema"
    profile = heuristic_topic_profile(topic)

    assert profile["project_type"] == "GENERAL_TOPIC"
    assert profile["locations"][0] == "Muzema"
    assert profile["search_synonyms"][0] == "produção habitacional milícia Muzema"
    assert topic in profile["search_synonyms"]

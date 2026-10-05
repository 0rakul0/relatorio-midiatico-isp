from app.media_scout import MediaScout
from app.source_registry import PRIORITY_MEDIA_SOURCES
from app.topic_profile import heuristic_topic_profile


def test_event_scout_uses_compact_strategy_instead_of_paraphrase_matrix():
    topic = "policiais mortos em agosto de 2026 no Rio de Janeiro"
    profile = heuristic_topic_profile(topic)
    tasks = MediaScout(topic, profile).web_tasks(max_complementary=2)

    thematic = [task for task in tasks if not task.is_priority]
    portals = [task for task in tasks if task.is_priority]

    assert len(thematic) <= 3
    assert thematic[0].role == "PRIMARY"
    assert len(portals) == len(PRIORITY_MEDIA_SOURCES)
    assert all(task.role == "PRIORITY_PORTAL" for task in portals)


def test_portal_queries_reuse_primary_query_instead_of_inventing_new_variants():
    topic = "Dossie Mulher 2026"
    profile = heuristic_topic_profile(topic)
    scout = MediaScout(topic, profile)
    tasks = scout.web_tasks(max_complementary=2)

    primary = next(task.query for task in tasks if task.role == "PRIMARY")
    portals = [task for task in tasks if task.role == "PRIORITY_PORTAL"]

    assert portals
    assert all(primary in task.query for task in portals)


def test_product_topic_keeps_product_anchor():
    topic = "Dossie Mulher"
    profile = heuristic_topic_profile(topic)
    tasks = MediaScout(topic, profile).web_tasks(max_complementary=2)
    queries = [task.query for task in tasks]

    assert profile["product_name"] == "Dossie Mulher"
    assert not any(query.strip().lower() in {"dossie", "mulher"} for query in queries)
    assert all(
        "dossie mulher" in task.query.lower()
        for task in tasks
        if task.role in {"PRIMARY", "PRIORITY_PORTAL"}
    )


def test_youtube_is_routed_from_the_main_web_discovery():
    topic = "Dossie Mulher"
    profile = heuristic_topic_profile(topic)
    status = MediaScout(topic, profile).platform_status(youtube_enabled=True)

    youtube = next(entry for entry in status if entry["platform"] == "YouTube")
    assert youtube["status"] == "roteado da descoberta DuckDuckGo"


def test_general_scout_prefers_canonical_location_variant():
    topic = "produção habitacional milícia mazuema"
    profile = heuristic_topic_profile(topic)
    tasks = MediaScout(topic, profile).web_tasks(max_complementary=2)

    primary = next(task.query for task in tasks if task.role == "PRIMARY")
    complementaries = [task.query for task in tasks if task.role == "COMPLEMENTARY"]

    assert "Muzema" in primary
    assert "mazuema" not in primary.lower()
    assert any("mazuema" in query.lower() for query in complementaries)


def test_general_scout_uses_contextual_profile_angles():
    topic = "polarização política no Brasil em 2026"
    profile = heuristic_topic_profile(topic)
    profile["actors"] = ["Lula", "Bolsonaro"]
    profile["actions"] = ["polarização eleitoral", "rejeição política"]
    profile["organizations"] = ["Datafolha"]

    scout = MediaScout(topic, profile)
    strategy = scout.fallback_search_strategy(max_complementary=4)
    queries = [strategy["primary_query"], *strategy["complementary_queries"]]

    assert len(queries) >= 4
    assert any("Lula" in query for query in queries)
    assert any("Bolsonaro" in query for query in queries)

from types import SimpleNamespace

from app.media_scout import MediaScout
from app.source_registry import PRIORITY_MEDIA_SOURCES, PRIORITY_YOUTUBE_CHANNELS
from app.services import youtube_tasks_for_execution
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


def test_youtube_uses_one_thematic_query_plus_priority_channels():
    topic = "Dossie Mulher"
    profile = heuristic_topic_profile(topic)
    tasks = MediaScout(topic, profile).youtube_tasks()

    thematic = [task for task in tasks if not task.is_priority]
    channels = [task for task in tasks if task.is_priority]

    assert len(thematic) == 1
    assert len(channels) == len(PRIORITY_YOUTUBE_CHANNELS)


def test_youtube_execution_preserves_every_priority_channel_when_budget_is_lower(monkeypatch):
    from app.services.collection import youtube_helpers

    monkeypatch.setattr(
        youtube_helpers,
        "get_settings",
        lambda: SimpleNamespace(max_youtube_tasks=1),
    )
    project = SimpleNamespace(
        topic="Dossie Mulher",
        topic_profile=heuristic_topic_profile("Dossie Mulher"),
    )
    tasks = youtube_tasks_for_execution(project)
    priority_targets = [task.target for task in tasks if task.is_priority]
    assert priority_targets == [label for label, _ in PRIORITY_YOUTUBE_CHANNELS]

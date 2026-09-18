from app.media_scout import MediaScout
from app.source_registry import PRIORITY_YOUTUBE_CHANNELS
from app.services import youtube_tasks_for_execution
from types import SimpleNamespace
from app.topic_profile import heuristic_topic_profile


def test_media_scout_does_not_use_only_exact_topic():
    topic = "policiais mortos em agosto de 2026 no Rio de Janeiro"
    profile = heuristic_topic_profile(topic)
    queries = [task.query for task in MediaScout(topic, profile).web_tasks()]
    assert topic in queries
    assert any(query != topic and "policial" in query.lower() for query in queries)
    assert any("odia.ig.com.br" in query for query in queries)


def test_product_topic_portal_queries_keep_product_name():
    topic = "Dossiê Mulher"
    profile = heuristic_topic_profile(topic)
    assert profile["product_name"] == "Dossiê Mulher"
    queries = [task.query for task in MediaScout(topic, profile).web_tasks()]
    portal_queries = [query for query in queries if query.startswith("site:")]
    assert portal_queries
    assert all("Dossiê Mulher" in query for query in portal_queries)


def test_product_topic_does_not_generate_isolated_generic_term():
    topic = "Dossiê Mulher"
    profile = heuristic_topic_profile(topic)
    queries = [task.query for task in MediaScout(topic, profile).web_tasks()]
    assert "dossie" not in queries
    assert '"Dossiê Mulher"' in queries
    assert 'dossie mulher' not in queries
    assert not any(query == "mulher" for query in queries)


def test_product_topic_youtube_channel_queries_keep_product_name():
    topic = "Dossiê Mulher"
    profile = heuristic_topic_profile(topic)
    tasks = MediaScout(topic, profile).youtube_tasks()
    channel_tasks = [task for task in tasks if task.is_priority]
    assert channel_tasks
    assert all("Dossiê Mulher" in task.query for task in channel_tasks)


def test_youtube_execution_preserves_every_priority_channel_when_budget_is_lower(monkeypatch):
    from app.services.collection import youtube_helpers

    monkeypatch.setattr(youtube_helpers, "get_settings", lambda: SimpleNamespace(max_youtube_tasks=1))
    project = SimpleNamespace(
        topic="Dossiê Mulher",
        topic_profile=heuristic_topic_profile("Dossiê Mulher"),
    )
    tasks = youtube_tasks_for_execution(project)
    assert [task.target for task in tasks] == [label for label, _ in PRIORITY_YOUTUBE_CHANNELS]

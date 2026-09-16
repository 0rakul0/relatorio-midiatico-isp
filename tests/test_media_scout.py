from app.media_scout import MediaScout
from app.topic_profile import heuristic_topic_profile


def test_media_scout_does_not_use_only_exact_topic():
    topic = "policiais mortos em agosto de 2026 no Rio de Janeiro"
    profile = heuristic_topic_profile(topic)
    queries = [task.query for task in MediaScout(topic, profile).web_tasks()]
    assert topic in queries
    assert any(query != topic and "policial" in query.lower() for query in queries)
    assert any("odia.ig.com.br" in query for query in queries)

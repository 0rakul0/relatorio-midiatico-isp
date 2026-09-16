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

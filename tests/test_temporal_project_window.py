from datetime import date
from types import SimpleNamespace

from app.topic_profile import requested_topic_window


def test_event_year_in_topic_resolves_full_year():
    assert requested_topic_window("operações policiais no estado do Rio de Janeiro em 2025") == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )

from datetime import date
from types import SimpleNamespace

from app.services import query_window


def project():
    return SimpleNamespace(
        collection_start=date(2026, 8, 1),
        collection_end=date(2026, 8, 31),
        event_start=date(2026, 8, 1),
        event_end=date(2026, 8, 31),
        fact_grace_days=10,
    )


def test_media_query_uses_media_window():
    start, end = query_window(project(), SimpleNamespace(purpose="MEDIA_REPERCUSSION"))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)


def test_fact_query_gets_confirmation_grace_period():
    start, end = query_window(project(), SimpleNamespace(purpose="FACT_DISCOVERY"))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 9, 10)


def test_official_query_is_not_limited_by_publication_date():
    start, end = query_window(project(), SimpleNamespace(purpose="OFFICIAL_FACT"))
    assert start is None
    assert end is None

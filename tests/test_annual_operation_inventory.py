from datetime import date
from types import SimpleNamespace

from app.services.search_planning import _annual_event_inventory_queries


def test_full_year_police_operations_get_monthly_fact_inventory(monkeypatch):
    project = SimpleNamespace(
        project_type="EVENT_TOPIC",
        event_start=date(2025, 1, 1),
        event_end=date(2025, 12, 31),
        topic="operações policiais no estado do Rio de Janeiro no ano de 2025",
        topic_profile={"event_anchor": "operações policiais", "locations": ["Rio de Janeiro"]},
    )
    rows = _annual_event_inventory_queries(project)
    assert len(rows) == 12
    assert any("janeiro 2025" in q for q, _ in rows)
    assert any("dezembro 2025" in q for q, _ in rows)

from datetime import date
from types import SimpleNamespace

from app.services.search_planning import _official_operation_inventory_queries


def test_full_year_operation_topic_searches_primary_sources_monthly():
    project = SimpleNamespace(
        event_start=date(2025, 1, 1),
        event_end=date(2025, 12, 31),
        topic="operações policiais no estado do Rio de Janeiro no ano de 2025",
        topic_profile={"event_anchor": "operações policiais"},
    )
    rows = _official_operation_inventory_queries(project)
    assert len(rows) == 36
    queries = [q for q, _kind, _reason in rows]
    assert any("site:sepm.rj.gov.br" in q and "janeiro 2025" in q for q in queries)
    assert any("site:policiacivil.rj.gov.br" in q and "dezembro 2025" in q for q in queries)
    assert any("site:rj.gov.br" in q and "junho 2025" in q for q in queries)

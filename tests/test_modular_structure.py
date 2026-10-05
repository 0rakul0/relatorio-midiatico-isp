from datetime import date
from types import SimpleNamespace

from app.fact_layer import _operation_display_name, event_identity_key, normalize_fact_value
from app.main import app
from app.search.guards import is_redundant
from app.services import search_planning


def test_fact_layer_facade_preserves_normalization_contract():
    assert normalize_fact_value("unit", "03º BPM") == normalize_fact_value("unit", "3 BPM")
    extracted = {
        "subject_name": {"value": "João da Silva"},
        "institution": {"value": "PMERJ"},
    }
    assert event_identity_key(extracted)


def test_fact_layer_facade_preserves_operation_display_helper():
    event = SimpleNamespace(
        operation_name=None,
        event_date=date(2025, 5, 3),
        neighborhood="Complexo X",
        city="Rio de Janeiro",
    )
    assert "2025-05-03" in _operation_display_name(event)


def test_search_planning_facade_preserves_redundancy_helper():
    assert is_redundant(
        '2026 "Rio de Janeiro" "policiais mortos"',
        ['"policiais mortos" "Rio de Janeiro" 2026'],
    )
    assert search_planning._is_redundant(
        '2026 "Rio de Janeiro" "policiais mortos"',
        ['"policiais mortos" "Rio de Janeiro" 2026'],
    )


def test_modular_api_routes_remain_registered():
    paths = {getattr(route, "path", None) for route in app.routes}
    expected = {
        "/costs",
        "/costs/summary",
        "/chat",
        "/chat/projects",
        "/reports/history",
        "/reports/cache",
    }
    assert expected.issubset(paths)

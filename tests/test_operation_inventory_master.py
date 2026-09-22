from datetime import date
from types import SimpleNamespace

from app.fact_layer import _operation_display_name


def test_operation_display_name_prefers_named_operation():
    event = SimpleNamespace(operation_name="Operação Contenção", event_date=date(2025,10,28), neighborhood="Penha", city="Rio de Janeiro")
    assert _operation_display_name(event) == "Operação Contenção"


def test_operation_display_name_builds_auditable_fallback():
    event = SimpleNamespace(operation_name=None, event_date=date(2025,5,3), neighborhood="Complexo X", city="Rio de Janeiro")
    assert "2025-05-03" in _operation_display_name(event)
    assert "Complexo X" in _operation_display_name(event)

from datetime import date
from types import SimpleNamespace
from app.services.corpus_reuse import _in_requested_window

def _project():
    return SimpleNamespace(has_custom_date_window=True, collection_start=date(2025,1,1), collection_end=date(2025,12,31))

def test_unknown_date_not_reused_in_custom_window():
    doc=SimpleNamespace(published_at=None,url="https://example.org/noticia",title="Operação policial")
    assert _in_requested_window(_project(),doc) is False

def test_2026_not_reused_in_2025_window():
    doc=SimpleNamespace(published_at=date(2026,2,1),url="",title="")
    assert _in_requested_window(_project(),doc) is False

def test_2025_reused_in_2025_window():
    doc=SimpleNamespace(published_at=date(2025,8,15),url="",title="")
    assert _in_requested_window(_project(),doc) is True

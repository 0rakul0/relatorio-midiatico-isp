from app.tools.academic import _dedup_key

def test_academic_dedup_prefers_doi():
    assert _dedup_key({"doi": "10.1590/ABC", "title": "A"}) == "doi:10.1590/abc"

def test_academic_dedup_falls_back_to_normalized_title():
    assert _dedup_key({"doi": None, "title": "  Polícia   e Sociedade "}) == "title:polícia e sociedade"

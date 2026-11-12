"""Regression tests for the optional institutional presence criterion."""
from types import SimpleNamespace

from app.api.institutional_presence import (
    Evidence, InstitutionalCriteria, InstitutionalFinding,
    evidence_status, plan_for_project,
)

def test_institutional_plan_preserves_existing_window():
    project = SimpleNamespace(
        id=42, institution="Instituto de Segurança Pública",
        collection_start="2025-01-01", collection_end="2025-12-31",
    )
    plan = plan_for_project(
        project, InstitutionalCriteria(), ["Dossiê Mulher"]
    )
    assert plan["collection_start"] == "2025-01-01"
    assert plan["collection_end"] == "2025-12-31"
    assert any("Dossiê Mulher" in q for q in plan["queries"])
    assert "reuse_existing_corpus" in plan["steps"]

def test_indirect_without_evidence_is_not_confirmed():
    item = InstitutionalFinding(
        title="Reportagem sobre dados de segurança",
        publication="Jornal", publication_date="2025-04-02",
        location="inside_page", classification="indirect",
        article_url="https://example.org/news", confirmed=True,
    )
    assert evidence_status(item) == "review"
    item.evidence = [Evidence(
        url="https://example.org/document",
        description="Fonte documental que demonstra a origem dos dados",
    )]
    assert evidence_status(item) == "confirmed"

def test_unrecognized_classification_stays_in_review():
    item = InstitutionalFinding(
        title="Notícia sobre segurança",
        publication="Jornal", publication_date="2025-04-02",
        location="digital", classification="unverified",
        article_url="https://example.org/news", confirmed=True,
        evidence=[Evidence(
            url="https://example.org/source",
            description="Documento de referência para auditoria",
        )],
    )
    assert evidence_status(item) == "review"

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

def test_archive_page_occurrence_is_pending_review():
    from app.api.institutional_presence import ArchiveBatch, ArchivePage, extract_archive_matches
    batch = ArchiveBatch(
        products=["Dossiê Mulher"],
        pages=[ArchivePage(
            publication="Jornal de teste", edition_date="2025-06-01",
            page_number=1, source_url="https://example.org/edition.pdf",
            text="Na edição deste jornal, o Dossiê Mulher foi citado em uma matéria sobre dados.",
        )],
    )
    result = extract_archive_matches(batch)
    assert result["pages_processed"] == 1
    assert any(x["editorial_location"] == "front_page" for x in result["matches"])
    assert all(x["status"] == "REVIEW_REQUIRED" for x in result["matches"])


def test_indirect_match_never_auto_confirms():
    from app.api.institutional_presence import verify_indirect_citation
    excerpt = "O documento apresenta indicadores de violência contra mulheres no estado."
    result = verify_indirect_citation(
        "Segundo a pesquisa: " + excerpt,
        excerpt, "https://example.org/official.pdf",
    )
    assert result["status"] == "EVIDENCE_MATCH_REVIEW_REQUIRED"

def test_press_only_execution_plan():
    from app.services.execution_profile import sanitize_execution_plan
    project = SimpleNamespace(
        execution_options={"institutional_presence": True,
                           "enable_academic_research": True,
                           "enable_fact_layer": True},
        execution_profile="AUTO",
        topic_profile={},
        project_type="AUTO",
    )
    plan = sanitize_execution_plan(project, None)
    processes = plan["processes"]
    for name in ("youtube_collection", "social_repercussion",
                 "academic_research", "fact_extraction",
                 "fact_resolution", "nominal_followup"):
        assert processes[name]["enabled"] is False


def test_empty_products_produce_default_press_queries():
    from app.api.institutional_presence import institutional_search_queries
    project = SimpleNamespace(
        institution="Instituto de Segurança Pública",
        execution_options={"institutional_presence": True,
                           "institutional_products": []},
    )
    queries = institutional_search_queries(project)
    assert any("Dossiê Mulher" in query for query in queries)
    assert all("youtube" not in query.lower() for query in queries)

def test_cover_queries_are_restricted_to_newspaper_archives():
    from app.api.institutional_presence import institutional_cover_queries
    project = SimpleNamespace(
        collection_start="2025-01-01", collection_end="2025-12-31"
    )
    queries = institutional_cover_queries(project)
    assert len(queries) == 6
    assert all(("capa" in q or "primeira página" in q) for q in queries)
    assert all("2025" in q for q in queries)
    assert not any("tiktok" in q.lower() or "youtube" in q.lower() for q in queries)

def test_front_page_preset_blocks_unrelated_modules():
    from app.services.execution_profile import sanitize_execution_plan
    project = SimpleNamespace(
        execution_profile="MANCHETES_CAPA",
        execution_options={"cover_only": True, "institutional_presence": True,
                           "enable_academic_research": True, "enable_fact_layer": True},
        topic_profile={}, project_type="AUTO",
    )
    processes = sanitize_execution_plan(project, {
        "processes": {"social_repercussion": {"enabled": True},
                      "youtube_collection": {"enabled": True}}
    })["processes"]
    for name in ("youtube_collection", "social_repercussion",
                 "academic_research", "fact_extraction", "fact_resolution",
                 "nominal_followup", "second_fact_pass"):
        assert processes[name]["enabled"] is False
    assert processes["report_writer"]["enabled"] is True
    assert processes["qa"]["enabled"] is True

def test_cover_pdf_excludes_social_and_public_opinion():
    from app.pdf_report import build_pdf
    data = {
        "report": {"title": "Teste"},
        "project": {
            "execution_profile": "MANCHETES_CAPA",
            "collection_start": "2025-01-01",
            "collection_end": "2025-12-31",
        },
        "metrics": {},
        "social_repercussion": {"post_inventory": [{"platform": "tiktok"}]},
        "public_opinion": {"surveys": [{"title": "Pesquisa"}]},
        "institutional_findings": [],
    }
    pdf = build_pdf(data)
    assert pdf.startswith(b"%PDF")
    from io import BytesIO
    from pypdf import PdfReader
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
    assert "Nenhuma manchete de capa" in text
    assert "TikTok" not in text
    assert "Pesquisa de opinião" not in text

from datetime import date
from types import SimpleNamespace

from app.fact_layer import _operation_display_name, event_identity_key, normalize_fact_value
from app.main import app
from app.database import Base
from app.models import (
    AppUser,
    MediaItem,
    Project,
    PublicOpinionSurvey,
    SocialComment,
)
from app.search.guards import is_redundant
from app.search.gap_fill import has_site_operator
from app.metrics.corpus import deduplicate_corpus as domain_deduplicate_corpus
from app.services.metrics import deduplicate_corpus as service_deduplicate_corpus
from app.social.discovery import social_discovery_terms
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
    assert has_site_operator("site:example.com tema")
    assert search_planning._has_site_operator("site:example.com tema")


def test_modular_api_routes_remain_registered():
    paths = {getattr(route, "path", None) for route in app.routes}
    expected = {
        "/costs",
        "/costs/summary",
        "/chat",
        "/chat/projects",
        "/reports/history",
        "/reports/cache",
        "/projects/{project_id}/run-async",
        "/projects/{project_id}/facts",
        "/projects/{project_id}/report",
        "/projects/{project_id}/export.pdf",
    }
    assert expected.issubset(paths)

def test_models_facade_registers_domain_tables():
    assert Project.__tablename__ == "projects"
    assert MediaItem.__tablename__ == "media_items"
    assert SocialComment.__tablename__ == "social_comments"
    assert PublicOpinionSurvey.__tablename__ == "public_opinion_surveys"
    assert AppUser.__tablename__ == "app_users"
    expected = {
        "projects",
        "media_items",
        "social_comments",
        "public_opinion_surveys",
        "app_users",
        "report_versions",
        "fact_events",
    }
    assert expected.issubset(set(Base.metadata.tables))

def test_metrics_facade_uses_domain_deduplication():
    rows = [
        {
            "title": "Notícia sobre o tema",
            "url": "https://example.com/a?utm_source=x",
            "canonical_url": "https://example.com/a?utm_source=x",
            "domain": "example.com",
            "media_origin": "PORTAL_NOTICIAS",
        },
        {
            "title": "Notícia sobre o tema",
            "url": "https://example.com/a",
            "canonical_url": "https://example.com/a",
            "domain": "example.com",
            "media_origin": "PORTAL_NOTICIAS",
        },
    ]
    assert service_deduplicate_corpus(rows) == domain_deduplicate_corpus(rows)
    assert len(service_deduplicate_corpus(rows)) == 1


def test_social_discovery_domain_keeps_compact_terms():
    project = SimpleNamespace(
        topic="Como está a polarização política no Brasil em 2026?",
        topic_profile={
            "search_synonyms": ["polarização política Brasil 2026"],
            "actors": ["Lula", "Bolsonaro"],
            "locations": ["Brasil"],
        },
        collection_end=date(2026, 10, 5),
    )
    terms = social_discovery_terms(project)
    assert terms
    assert any("Lula Bolsonaro" in term for term in terms)
    assert all(len(term.split()) <= 8 for term in terms)


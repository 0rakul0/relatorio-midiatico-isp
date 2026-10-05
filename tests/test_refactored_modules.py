from datetime import date, datetime

from app.prompts.report_agent import BASE_PROMPT, TASK_PROMPTS
from app.reports.pdf.helpers import window_label
from app.social.dates import duckduckgo_result_date
from app.social.sampling import balanced_sample
from app.social.urls import platform_for_url
from app.utils.rendering import has_content, text


def test_refactored_prompt_registry_keeps_core_tasks():
    assert BASE_PROMPT.strip()
    for task in ("topic_profile", "report_writer", "qa", "public_opinion_extraction"):
        assert task in TASK_PROMPTS
        assert TASK_PROMPTS[task].strip()


def test_rendering_helpers_remain_deterministic():
    assert text("A—B") == "A-B"
    assert has_content({"x": ["", 1]}) is True
    assert has_content({"x": ["", 0]}) is False
    assert window_label("2026-01-01", "2026-01-31") == "2026-01-01 a 2026-01-31"


def test_social_domain_helpers_keep_public_contracts():
    assert platform_for_url("https://x.com/exemplo/status/123456789") == "x"
    published_at, source, raw = duckduckgo_result_date({
        "published_at": "3 days ago",
        "retrieved_at": "2026-10-05T10:00:00+00:00",
    })
    assert published_at == date(2026, 10, 2)
    assert source == "duckduckgo_relative_date"
    assert raw == "3 days ago"


def test_balanced_sample_accepts_empty_input():
    assert balanced_sample([], 120) == []

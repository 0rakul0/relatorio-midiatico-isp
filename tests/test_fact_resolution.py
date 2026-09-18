from datetime import date
from types import SimpleNamespace

from app.fact_layer import (
    _identity_conflicts,
    resolve_assertions,
    fact_event_exclusion_reason,
    normalize_person_name,
)
from app.report_qa import deterministic_report_qa


def assertion(value, url, source_type="MEDIA"):
    return SimpleNamespace(
        value_text=value,
        evidence="evidência",
        evidence_status="SUPPORTED",
        source_url=url,
        source_type=source_type,
    )


def test_normalize_person_name():
    assert normalize_person_name("  João   da SILVA ") == "joao da silva"


def _stored_event(**overrides):
    base = {
        "event_date": None,
        "death_date": None,
        "institution": None,
        "unit": None,
        "city": None,
        "state": None,
        "rank_or_role": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _extracted(**fields):
    return {name: {"value": value} for name, value in fields.items()}


def test_same_name_with_different_location_does_not_merge():
    event = _stored_event(city="Niterói")
    assert _identity_conflicts(event, _extracted(city="Rio de Janeiro")) is True


def test_missing_attributes_do_not_block_merge():
    event = _stored_event(city="Niterói")
    assert _identity_conflicts(event, _extracted(subject_name="João")) is False


def test_matching_date_and_institution_confirm_same_identity():
    event = _stored_event(event_date=date(2026, 8, 10), institution="PMERJ")
    assert _identity_conflicts(
        event, _extracted(event_date="2026-08-10", institution="PMERJ")
    ) is False


def test_same_name_different_date_does_not_merge():
    event = _stored_event(event_date=date(2026, 8, 10))
    assert _identity_conflicts(event, _extracted(event_date="2026-08-11")) is True


def test_two_independent_sources_confirm_same_value():
    value, status, conflicts = resolve_assertions(
        "event_date",
        [
            assertion("2026-08-08", "https://a.example/x"),
            assertion("2026-08-08", "https://b.example/y"),
        ],
    )
    assert value == "2026-08-08"
    assert status == "CONFIRMED"
    assert conflicts == []


def test_conflicting_dates_are_not_silently_resolved():
    value, status, conflicts = resolve_assertions(
        "event_date",
        [
            assertion("2026-08-29", "https://a.example/x"),
            assertion("2026-08-30", "https://b.example/y"),
        ],
    )
    assert value is None
    assert status == "SOURCE_CONFLICT"
    assert set(conflicts) == {"2026-08-29", "2026-08-30"}


def test_official_source_can_confirm_single_assertion():
    value, status, _ = resolve_assertions(
        "institution",
        [assertion("PMERJ", "https://sepm.rj.gov.br/nota", source_type="OFFICIAL")],
    )
    assert value == "PMERJ"
    assert status == "CONFIRMED"


def test_fact_outside_requested_month_is_excluded_from_main_report():
    project = SimpleNamespace(event_start=date(2026, 8, 1), event_end=date(2026, 8, 31))
    event = SimpleNamespace(
        event_date=date(2026, 9, 14),
        death_date=None,
        primary_scope=True,
        resolution_status="CONFIRMED",
    )
    assert fact_event_exclusion_reason(project, event) == "FATO_POSTERIOR_A_JANELA_SOLICITADA"


def test_fact_without_confirmed_date_is_excluded_from_main_report():
    project = SimpleNamespace(event_start=date(2026, 8, 1), event_end=date(2026, 8, 31))
    event = SimpleNamespace(
        event_date=None,
        death_date=None,
        primary_scope=True,
        resolution_status="CONFIRMED",
    )
    assert fact_event_exclusion_reason(project, event) == "DATA_DO_FATO_NAO_CONFIRMADA"


def test_conflicting_fact_is_excluded_even_when_its_date_is_in_scope():
    project = SimpleNamespace(event_start=date(2026, 8, 1), event_end=date(2026, 8, 31))
    event = SimpleNamespace(
        event_date=date(2026, 8, 15),
        death_date=None,
        primary_scope=True,
        resolution_status="SOURCE_CONFLICT",
    )
    assert fact_event_exclusion_reason(project, event) == "FATO_SEM_RESOLUCAO_CONFIAVEL"


def test_qa_rejects_an_excluded_fact_if_a_future_path_sends_it_to_the_report():
    findings = deterministic_report_qa(
        {
            "report": {},
            "metrics": {},
            "fact_events": [{"id": 42, "report_exclusion_reason": "FATO_POSTERIOR_A_JANELA_SOLICITADA"}],
        }
    )
    assert any(finding["code"] == "EXCLUDED_FACT_IN_MAIN_REPORT" for finding in findings)

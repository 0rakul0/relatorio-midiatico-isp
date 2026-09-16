from app.report_qa import deterministic_report_qa


def base_payload():
    return {
        "project": {
            "collection_start": "2026-08-01",
            "collection_end": "2026-08-31",
            "event_start": "2026-08-01",
            "event_end": "2026-08-31",
        },
        "metrics": {"valid_items": 0},
        "fact_events": [],
        "report": {
            "executive_summary": "Nenhum item validado na amostra auditável.",
            "synthesis": "A amostra não permitiu validar itens.",
        },
    }


def test_zero_items_safe_wording_passes():
    findings = deterministic_report_qa(base_payload())
    assert not [x for x in findings if x["code"] == "ABSENCE_OVERCLAIM"]


def test_zero_items_cannot_become_no_coverage_claim():
    payload = base_payload()
    payload["report"]["synthesis"] = "Não houve cobertura jornalística no período."
    findings = deterministic_report_qa(payload)
    assert any(x["code"] == "ABSENCE_OVERCLAIM" for x in findings)


def test_no_fact_cannot_become_no_occurrence_claim():
    payload = base_payload()
    payload["report"]["synthesis"] = "Não houve ocorrência no período."
    findings = deterministic_report_qa(payload)
    assert any(x["code"] == "FACT_ABSENCE_OVERCLAIM" for x in findings)

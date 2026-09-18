from app.schemas import ReportPlanResponse


def test_report_plan_schema_accepts_compact_dynamic_plan():
    row = ReportPlanResponse.model_validate(
        {
            "web_collection": {"enabled": True, "reason": "base do relatorio"},
            "youtube_collection": {"enabled": False, "reason": "nao agrega ao tema"},
            "cross_validation": {"enabled": False, "reason": "youtube desativado"},
            "media_validation": {"enabled": True, "reason": "obrigatoria"},
            "fact_extraction": {"enabled": False, "reason": "tema predominantemente tematico"},
            "fact_resolution": {"enabled": False, "reason": "sem fatos individuais"},
            "nominal_followup": {"enabled": False, "reason": "nao exige pessoas"},
            "second_fact_pass": {"enabled": False, "reason": "sem busca nominal"},
            "classification": {"enabled": True, "reason": "obrigatoria"},
            "report_writer": {"enabled": True, "reason": "obrigatoria"},
            "qa": {"enabled": True, "reason": "obrigatoria"},
            "fact_fields": [],
            "primary_query": '"drones" faccoes criminosas "Rio de Janeiro"',
            "complementary_queries": [],
            "fact_query": None,
            "official_query": None,
            "rationale": "Plano enxuto para analise tematica de repercussao.",
        }
    )
    assert row.nominal_followup.enabled is False
    assert row.primary_query

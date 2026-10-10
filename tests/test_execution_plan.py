from types import SimpleNamespace

from app.services.execution_profile import sanitize_execution_plan


def _project(**kwargs):
    base = dict(
        execution_profile="AUTO",
        execution_options={},
        execution_plan={},
        project_type="GENERAL_TOPIC",
        topic_profile={"requested_fact_fields": []},
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _raw_plan(**overrides):
    processes = {
        "web_collection": {"enabled": True, "reason": "required"},
        "youtube_collection": {"enabled": True, "reason": "useful"},
        "cross_validation": {"enabled": True, "reason": "video enabled"},
        "media_validation": {"enabled": True, "reason": "required"},
        "fact_extraction": {"enabled": False, "reason": "thematic topic"},
        "fact_resolution": {"enabled": False, "reason": "no fact extraction"},
        "nominal_followup": {"enabled": False, "reason": "not person-centric"},
        "second_fact_pass": {"enabled": False, "reason": "no nominal collection"},
        "classification": {"enabled": True, "reason": "required"},
        "report_writer": {"enabled": True, "reason": "required"},
        "qa": {"enabled": True, "reason": "required"},
    }
    processes.update(overrides)
    return {"processes": processes, "fact_fields": [], "rationale": "compact plan"}


def test_drones_style_plan_does_not_force_nominal_followup():
    project = _project()
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["fact_extraction"]["enabled"] is False
    assert plan["processes"]["nominal_followup"]["enabled"] is False
    assert plan["processes"]["second_fact_pass"]["enabled"] is False


def test_dependencies_disable_nominal_when_fact_layer_is_disabled():
    project = _project()
    raw = _raw_plan(
        nominal_followup={"enabled": True, "reason": "model mistake"},
        second_fact_pass={"enabled": True, "reason": "model mistake"},
        fact_resolution={"enabled": True, "reason": "model mistake"},
    )
    plan = sanitize_execution_plan(project, raw)
    assert plan["processes"]["fact_resolution"]["enabled"] is False
    assert plan["processes"]["nominal_followup"]["enabled"] is False
    assert plan["processes"]["second_fact_pass"]["enabled"] is False


def test_explicit_simple_preset_wins_over_auto_planner():
    project = _project(execution_profile="MIDIATICO_SIMPLES")
    raw = _raw_plan(
        fact_extraction={"enabled": True, "reason": "model wanted facts"},
        nominal_followup={"enabled": True, "reason": "model wanted names"},
    )
    plan = sanitize_execution_plan(project, raw)
    assert plan["processes"]["fact_extraction"]["enabled"] is False
    assert plan["processes"]["nominal_followup"]["enabled"] is False


def test_media_routing_is_structural_even_for_simple_profile():
    project = _project(execution_profile="MIDIATICO_SIMPLES")
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["youtube_collection"]["enabled"] is True
    assert plan["processes"]["social_repercussion"]["enabled"] is True


def test_legacy_social_override_does_not_disable_structural_routing():
    project = _project(
        execution_options={"enable_social_repercussion": False}
    )
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["social_repercussion"]["enabled"] is True


def test_scientific_profile_runs_academic_research_without_nominal_collection():
    project = _project(execution_profile="MIDIATICO_CIENTIFICO")
    plan = sanitize_execution_plan(project, _raw_plan(
        academic_research={"enabled": False, "reason": "agent skipped"},
    ))
    assert plan["mode"] == "EXPLICIT_PRESET"
    assert plan["processes"]["academic_research"]["enabled"] is True
    assert plan["processes"]["web_collection"]["enabled"] is True
    assert plan["processes"]["fact_extraction"]["enabled"] is False
    assert plan["processes"]["nominal_followup"]["enabled"] is False


def test_scientific_profile_cannot_disable_academic_search_by_override():
    project = _project(
        execution_profile="MIDIATICO_CIENTIFICO",
        execution_options={"enable_academic_research": False},
    )
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["academic_research"]["enabled"] is True


def test_project_create_accepts_scientific_profile():
    from app.schema_groups.api import ProjectCreate
    model = ProjectCreate(topic="Violencia contra mulheres no Brasil", execution_profile="MIDIATICO_CIENTIFICO")
    assert model.execution_profile == "MIDIATICO_CIENTIFICO"


def test_pdf_opinion_table_retains_auditable_indicators_outside_summary():
    from pathlib import Path
    # Regression contract: summary table must stay bounded rather than
    # occupying most of the document with repeated survey indicators.
    from app.reports.pdf import public_opinion
    import inspect
    source = inspect.getsource(public_opinion.add_public_opinion_section)
    assert "seen_indicators" in source
    assert "indicator_rows[:19]" in source


def test_scientific_profile_guidance_is_explicit_and_keeps_academic_enabled():
    from app.services.execution_profile import execution_profile_guidance
    project = _project(execution_profile="MIDIATICO_CIENTIFICO")
    guidance = execution_profile_guidance(project)
    assert "academic_research=true" in guidance
    assert "SciELO" in guidance
    plan = sanitize_execution_plan(project, _raw_plan(
        academic_research={"enabled": False, "reason": "model declined"}
    ))
    assert plan["processes"]["academic_research"]["enabled"]


def test_automatic_profile_has_distinct_guidance():
    from app.services.execution_profile import execution_profile_guidance
    assert execution_profile_guidance(_project(execution_profile="AUTO")) != execution_profile_guidance(
        _project(execution_profile="MIDIATICO_CIENTIFICO")
    )

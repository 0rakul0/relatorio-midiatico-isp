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


def test_social_repercussion_override_can_enable_paid_stage():
    project = _project(
        execution_options={"enable_social_repercussion": True}
    )
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["social_repercussion"]["enabled"] is True


def test_explicit_profile_keeps_social_repercussion_off_by_default():
    project = _project(execution_profile="MIDIATICO_SIMPLES")
    plan = sanitize_execution_plan(project, _raw_plan())
    assert plan["processes"]["social_repercussion"]["enabled"] is False

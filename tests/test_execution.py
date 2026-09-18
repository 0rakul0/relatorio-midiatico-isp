from app.orchestration import (
    RunCancelled,
    check_cancelled,
    create_run,
    mark_run_started,
    request_cancel,
    run_snapshot,
    update_stage,
)


def test_stage_lifecycle_colors_statuses():
    run = create_run(123)
    mark_run_started(run.run_id)
    update_stage(run.run_id, "profile", "RUNNING", "perfilando")
    snapshot = run_snapshot(run.run_id)
    profile = snapshot["stages"][0]
    assert profile["status"] == "RUNNING"
    assert profile["detail"] == "perfilando"

    update_stage(run.run_id, "profile", "DONE", "ok")
    snapshot = run_snapshot(run.run_id)
    assert snapshot["stages"][0]["status"] == "DONE"


def test_cancel_request_is_cooperative():
    run = create_run(456)
    mark_run_started(run.run_id)
    assert request_cancel(run.run_id) is True
    try:
        check_cancelled(run.run_id)
    except RunCancelled:
        pass
    else:
        raise AssertionError("cancelamento deveria interromper no próximo check")

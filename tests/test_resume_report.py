from pathlib import Path

def test_resume_endpoint_exists():
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "/projects/{project_id}/resume-async" in main
    assert "resume_run(project_id)" in main

def test_resume_does_not_call_full_pipeline():
    executor = Path("app/orchestration/executor.py").read_text(encoding="utf-8")
    body = executor.split("def _resume_report_worker", 1)[1].split("def resume_run", 1)[0]
    assert "run_full_methodology(" not in body
    assert "draft_report_with_llm" in body
    assert "run_report_qa" in body
    assert "refine_report_with_qa" in body

from app.llm import _is_reasoning_model


def test_reasoning_model_detection():
    assert _is_reasoning_model("gpt-5-mini")
    assert _is_reasoning_model("GPT-5.2")
    assert _is_reasoning_model("o3")
    assert _is_reasoning_model("o4-mini")
    assert not _is_reasoning_model("gpt-4.1-mini")
    assert not _is_reasoning_model("gpt-4o")
    assert not _is_reasoning_model("")

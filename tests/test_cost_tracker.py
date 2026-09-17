from app.cost_tracker import cost_context, emit, estimate_cost, set_record_sink


def test_estimate_cost_uses_model_prices():
    # gpt-5-mini: $0.25/M input, $2.00/M output.
    cost = estimate_cost(
        "gpt-5-mini",
        input_tokens=1000,
        output_tokens=500,
        cached_input_tokens=500,
    )
    expected = 0.0010 * 0.25 + 0.0005 * 2.0 + 0.0005 * 0.1 * 0.25
    assert abs(cost - expected) < 1e-9


def test_estimate_cost_charges_web_search_calls():
    cost = estimate_cost("gpt-5-nano", input_tokens=0, output_tokens=0, search_calls=1)
    assert cost == 0.010


def test_estimate_cost_falls_back_to_generic_price_for_unknown_model():
    cost = estimate_cost("modelo-desconhecido", input_tokens=1_000_000)
    assert cost == 0.15


def test_cost_context_attributes_and_sink(monkeypatch):
    recorded: list[dict] = []
    set_record_sink(lambda **kw: recorded.append(kw))

    with cost_context(project_id=7, operation="classify", schema_name="cls"):
        emit(model="gpt-5-mini", caller="structured_response", success=True)

    assert len(recorded) == 1
    assert recorded[0]["model"] == "gpt-5-mini"
    assert recorded[0]["caller"] == "structured_response"
    assert recorded[0]["project_id"] == 7
    assert recorded[0]["operation"] == "classify"

    # Após sair do contexto, as atribuições se perdem.
    recorded.clear()
    emit(model="gpt-5-mini", caller="structured_response", success=False)
    assert recorded[0]["project_id"] is None
    assert recorded[0]["operation"] is None
    assert recorded[0]["success"] is False


def test_cost_context_only_project_keeps_project():
    recorded: list[dict] = []
    set_record_sink(lambda **kw: recorded.append(kw))

    with cost_context(project_id=11):
        emit(model="gpt-5-mini", caller="x", success=True)
    assert recorded[0]["project_id"] == 11
    assert recorded[0]["operation"] is None
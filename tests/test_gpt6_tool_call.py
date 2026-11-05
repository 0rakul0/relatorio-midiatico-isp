"""GPT-6 Luna tool calling on /v1/chat/completions.

OpenAI requires an explicit reasoning_effort='none' with function tools.
A missing parameter also fails because the model has a reasoning default.
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from app.agent import ReportAgent
from app.llm import _is_reasoning_model, create_chat_model, tool_decision_kwargs


@tool
def pesquisar_exemplo(query: str) -> str:
    """Uma ferramenta de busca simulada para testes locais."""
    return query


class _Result(BaseModel):
    status: str


def test_luna_needs_explicit_none_for_tools():
    assert tool_decision_kwargs("gpt-6-luna") == {"reasoning_effort": "none"}
    assert tool_decision_kwargs("gpt-6-luna-2026-10-01") == {"reasoning_effort": "none"}
    assert tool_decision_kwargs("gpt-6-sol") == {"reasoning_effort": "none"}
    assert tool_decision_kwargs("gpt-6.1-sol") == {}
    assert tool_decision_kwargs("gpt-4.1-mini") == {}
    assert _is_reasoning_model("gpt-6-luna")


def test_langchain_tool_binding_overrides_base_reasoning_only_for_tools(monkeypatch):
    settings = create_chat_model.__globals__["get_settings"]().model_copy(
        update={
            "openai_model": "gpt-6-luna",
            "openai_reasoning_effort": "low",
            "openai_api_key": "dummy",
            "openai_only": True,
            "openai_base_url": None,
        }
    )
    monkeypatch.setattr("app.llm.get_settings", lambda: settings)
    model = create_chat_model(max_output_tokens=128)
    assert model.reasoning_effort == "low"
    bound = model.bind_tools(
        [pesquisar_exemplo], **tool_decision_kwargs(settings.openai_model)
    )
    assert bound.kwargs["reasoning_effort"] == "none"
    assert model.reasoning_effort == "low"


def test_agent_passes_none_only_to_tool_choice(monkeypatch):
    captured = {}
    class FakeBound:
        def invoke(self, messages):
            return AIMessage(content="Sem necessidade de ferramenta.")
    class FakeLLM:
        def bind_tools(self, tools, **kwargs):
            captured["kwargs"] = kwargs
            captured["tools"] = tools
            return FakeBound()
    llm = FakeLLM()
    monkeypatch.setattr("app.agent.create_chat_model", lambda **kwargs: llm)
    monkeypatch.setattr(
        "app.agent.get_settings",
        lambda: SimpleNamespace(openai_model="gpt-6-luna", max_agent_tool_rounds=1),
    )
    monkeypatch.setattr("app.agent.record_llm_usage", lambda **kwargs: None)
    def fake_finalize(self, **kwargs):
        captured["final_llm"] = kwargs["llm"]
        return {"status": "COMPLETED"}
    monkeypatch.setattr(ReportAgent, "_finalize", fake_finalize)
    result = ReportAgent().run(
        task="topic_profile",
        payload={"topic": "exemplo"},
        response_model=_Result,
        tools=[pesquisar_exemplo],
    )
    assert result["status"] == "COMPLETED"
    assert captured["kwargs"] == {"reasoning_effort": "none"}
    assert captured["final_llm"] is llm

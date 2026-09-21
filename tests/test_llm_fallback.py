"""Fallback OpenAI -> LLM local (app.llm)."""

from __future__ import annotations

import httpx
import langchain_openai
import openai
import pytest
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.messages import AIMessage

from app.llm import FallbackChatOpenAI, _is_retryable_openai_error, create_chat_model


def _openai_error(cls: type, status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://examples.invalid")
    return cls(
        "boom",
        response=httpx.Response(status_code=status, request=request),
        body={"detail": "x"},
    )


def _ok_result() -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=AIMessage(content="fallback ok"))])


def _patch_generate(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(langchain_openai.ChatOpenAI, "_generate", fn)


class TestRetryable:
    def test_429_rate_limit(self):
        assert _is_retryable_openai_error(_openai_error(openai.RateLimitError, 429))

    def test_401_auth(self):
        assert _is_retryable_openai_error(_openai_error(openai.AuthenticationError, 401))

    def test_5xx(self):
        assert _is_retryable_openai_error(_openai_error(openai.InternalServerError, 500))

    def test_connection_error(self):
        assert _is_retryable_openai_error(
            openai.APIConnectionError(request=httpx.Request("POST", "http://examples.invalid"))
        )

    def test_payload_errors_nao_engolidos(self):
        assert not _is_retryable_openai_error(_openai_error(openai.BadRequestError, 400))
        assert not _is_retryable_openai_error(_openai_error(openai.UnprocessableEntityError, 422))


class TestFallbackChatOpenAI:
    def test_fallback_usado_quando_primario_falha(self, monkeypatch):
        _patch_generate(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(_openai_error(openai.RateLimitError, 429)))

        class FakeFallback:
            @staticmethod
            def _generate(messages, stop=None, run_manager=None, **kwargs):
                return _ok_result()

        model = FallbackChatOpenAI(
            api_key="dummy", model="primario", temperature=0, max_completion_tokens=64
        )
        model._fallback_model = FakeFallback()
        result = model.invoke([("user", "oi")])
        assert result.content == "fallback ok"

    def test_erro_nao_retryable_propaga(self, monkeypatch):
        _patch_generate(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(_openai_error(openai.BadRequestError, 400)))

        model = FallbackChatOpenAI(
            api_key="dummy", model="primario", temperature=0, max_completion_tokens=64
        )
        model._fallback_model = None
        with pytest.raises(openai.BadRequestError):
            model.invoke([("user", "oi")])

    def test_sem_fallback_propaga_erro_original(self, monkeypatch):
        _patch_generate(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(_openai_error(openai.RateLimitError, 429)))

        model = FallbackChatOpenAI(
            api_key="dummy", model="primario", temperature=0, max_completion_tokens=64
        )
        model._fallback_model = None
        with pytest.raises(openai.RateLimitError):
            model.invoke([("user", "oi")])


class TestWiring:
    def _settings(self, monkeypatch: pytest.MonkeyPatch, **updates):
        base = create_chat_model.__globals__["get_settings"]()
        settings = base.model_copy(update={"openai_api_key": "dummy", **updates})
        monkeypatch.setattr("app.llm.get_settings", lambda: settings)
        return settings

    def test_create_chat_model_volta_fallback_when_configured(self, monkeypatch):
        self._settings(
            monkeypatch,
            openai_model="gpt-5-mini",
            openai_fallback_base_url="http://localhost:11434/v1",
            openai_fallback_model="gemma4:12b",
        )
        model = create_chat_model(max_output_tokens=64)
        assert isinstance(model, FallbackChatOpenAI)
        assert model._fallback_model is not None
        assert model._fallback_model.model_name == "gemma4:12b"

    def test_create_chat_model_sem_fallback(self, monkeypatch):
        self._settings(
            monkeypatch,
            openai_model="gpt-5-mini",
            openai_fallback_base_url=None,
            openai_fallback_model=None,
        )
        model = create_chat_model(max_output_tokens=64)
        assert model._fallback_model is None

    def test_fallback_ignorado_se_mesmo_endpoint(self, monkeypatch):
        self._settings(
            monkeypatch,
            openai_model="gpt-5-mini",
            openai_base_url="http://localhost:11434/v1",
            openai_fallback_base_url="http://localhost:11434/v1",
            openai_fallback_model="gemma4:12b",
        )
        model = create_chat_model(max_output_tokens=64)
        assert model._fallback_model is None

    def test_sem_chave_continua_levantando(self, monkeypatch):
        self._settings(monkeypatch, openai_api_key=None)
        with pytest.raises(RuntimeError):
            create_chat_model(max_output_tokens=64)
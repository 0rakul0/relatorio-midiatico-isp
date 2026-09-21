"""Infraestrutura mínima de acesso ao modelo.

Este módulo NÃO contém lógica de agente, prompts ou execução de tools.
Essas responsabilidades pertencem a ``app.agent``. Aqui ficam apenas:
- verificação de configuração;
- criação do ChatOpenAI;
- leitura de usage metadata;
- registro de custo/telemetria.
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.cost_tracker import emit

logger = logging.getLogger("app.llm")


def llm_is_configured() -> bool:
    return bool(get_settings().openai_api_key)


def _is_retryable_openai_error(exc: Exception) -> bool:
    """True quando a OpenAI está indisponível e o fallback local vale a pena.

    Cobre: 429 (rate limit, incl. credit_balance_exhausted), 5xx, 401 (chave
    inválida/revogada) e falhas de conexão/timeout. Não engole erros de
    validação de payload (400/422/408), que o fallback repetiria em vão.
    """
    if isinstance(exc, openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code in (401, 429) or exc.status_code >= 500
    return False


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def create_chat_model(*, max_output_tokens: int = 50000):
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    base_kwargs: dict = {
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
        "temperature": 0,
        "max_completion_tokens": max_output_tokens,
    }
    if settings.openai_base_url:
        base_kwargs["base_url"] = settings.openai_base_url

    fallback_model: ChatOpenAI | None = _build_fallback_model(
        max_output_tokens=max_output_tokens
    )

    effort = (settings.openai_reasoning_effort or "").strip().lower()
    if effort and _is_reasoning_model(settings.openai_model):
        try:
            return FallbackChatOpenAI(
                **base_kwargs, reasoning_effort=effort, fallback=fallback_model
            )
        except TypeError:
            # Versões antigas do langchain-openai sem o parâmetro dedicado.
            base_kwargs["model_kwargs"] = {"reasoning_effort": effort}
    return FallbackChatOpenAI(**base_kwargs, fallback=fallback_model)


def _build_fallback_model(*, max_output_tokens: int):
    settings = get_settings()
    fallback_base = (settings.openai_fallback_base_url or "").strip()
    fallback_model = (settings.openai_fallback_model or "").strip()
    if not fallback_base or not fallback_model:
        return None
    if fallback_base == (settings.openai_base_url or "").strip():
        return None
    return ChatOpenAI(
        api_key=settings.openai_api_key,
        model=fallback_model,
        temperature=0,
        max_completion_tokens=max_output_tokens,
        base_url=fallback_base,
    )


class FallbackChatOpenAI(ChatOpenAI):
    """ChatOpenAI que cai para um endpoint local quando a OpenAI falha.

    A troca acontece no nível de ``_generate``, então vale para chamadas
    diretas, ``bind_tools`` e ``with_structured_output`` (a finalização
    estruturada e as decisões de ferramentas passam por aqui).
    """

    def __init__(self, *args, fallback: ChatOpenAI | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._fallback_model = fallback

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> Any:
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:  # noqa: BLE001 - checado por _is_retryable_openai_error
            if not self._fallback_model or not _is_retryable_openai_error(exc):
                raise
            logger.warning(
                "OpenAI indisponível (%s: %s) - usando fallback %s",
                type(exc).__name__,
                exc,
                getattr(self._fallback_model, "model_name", "fallback"),
            )
            return self._fallback_model._generate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )


def usage_counts(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None) or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)

    input_details = usage.get("input_token_details") or {}
    cached_input_tokens = int(
        input_details.get("cache_read", 0)
        or input_details.get("cached_tokens", 0)
        or 0
    )

    if not input_tokens and not output_tokens:
        metadata = getattr(message, "response_metadata", None) or {}
        token_usage = metadata.get("token_usage") or metadata.get("usage") or {}
        input_tokens = int(
            token_usage.get("prompt_tokens", 0)
            or token_usage.get("input_tokens", 0)
            or 0
        )
        output_tokens = int(
            token_usage.get("completion_tokens", 0)
            or token_usage.get("output_tokens", 0)
            or 0
        )
        prompt_details = token_usage.get("prompt_tokens_details") or {}
        cached_input_tokens = int(
            prompt_details.get("cached_tokens", 0)
            or cached_input_tokens
            or 0
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def record_llm_usage(
    *,
    caller: str,
    success: bool,
    error: str | None = None,
    schema_name: str | None = None,
    **counts: int,
) -> None:
    emit(
        model=get_settings().openai_model,
        caller=caller,
        success=success,
        error=error,
        schema_name=schema_name,
        **counts,
    )

"""Infraestrutura mínima de acesso ao modelo.

Este módulo NÃO contém lógica de agente, prompts ou execução de tools.
Essas responsabilidades pertencem a ``app.agent``. Aqui ficam apenas:
- verificação de configuração;
- criação do ChatOpenAI;
- leitura de usage metadata;
- registro de custo/telemetria.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.cost_tracker import emit


def llm_is_configured() -> bool:
    return bool(get_settings().openai_api_key)


def _is_reasoning_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


def create_chat_model(*, max_output_tokens: int = 50000):
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    from langchain_openai import ChatOpenAI

    base_kwargs: dict = {
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
        "temperature": 0,
        "max_completion_tokens": max_output_tokens,
    }
    effort = (settings.openai_reasoning_effort or "").strip().lower()
    if effort and _is_reasoning_model(settings.openai_model):
        try:
            return ChatOpenAI(**base_kwargs, reasoning_effort=effort)
        except TypeError:
            # Versões antigas do langchain-openai sem o parâmetro dedicado.
            base_kwargs["model_kwargs"] = {"reasoning_effort": effort}
    return ChatOpenAI(**base_kwargs)


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

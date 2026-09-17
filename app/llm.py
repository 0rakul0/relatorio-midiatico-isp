"""Camada OpenAI sem ferramentas: recebe somente o contexto explicitamente fornecido."""

import json
from typing import Any

from app.config import get_settings
from app.cost_tracker import emit


def llm_is_configured() -> bool:
    return bool(get_settings().openai_api_key)


def _usage_counts(usage: Any) -> dict[str, int]:
    """Extrai contadores de tokens do objeto ``usage`` da Responses API."""
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
        }
    input_details = getattr(usage, "input_tokens_details", None)
    cached = 0
    if input_details is not None:
        cached = int(getattr(input_details, "cached_tokens", 0) or 0)
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cached_input_tokens": cached,
    }


def _record(
    *,
    model: str,
    caller: str,
    success: bool,
    error: str | None = None,
    schema_name: str | None = None,
    **counts: int,
) -> None:
    emit(
        model=model,
        caller=caller,
        success=success,
        error=error,
        schema_name=schema_name,
        **counts,
    )


def structured_response(
    *,
    instructions: str,
    payload: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
    max_output_tokens: int = 5000,
) -> dict[str, Any]:
    """Executa uma resposta estruturada JSON Schema usando exclusivamente OpenAI."""
    settings = get_settings()
    model = settings.openai_model
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    from openai import APIStatusError, OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=False),
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            max_output_tokens=max_output_tokens,
            store=False,
        )
    except APIStatusError as exc:
        _record(
            model=model,
            caller="structured_response",
            success=False,
            error=exc.message,
            schema_name=schema_name,
        )
        raise RuntimeError(f"Falha na OpenAI: {exc.message}") from exc

    counts = _usage_counts(getattr(response, "usage", None))
    if response.output_text:
        _record(model=model, caller="structured_response", success=True, schema_name=schema_name, **counts)
    else:
        _record(
            model=model,
            caller="structured_response",
            success=False,
            error="resposta sem conteúdo estruturado",
            schema_name=schema_name,
            **counts,
        )
        raise RuntimeError("A OpenAI não retornou conteúdo estruturado")
    return json.loads(response.output_text)

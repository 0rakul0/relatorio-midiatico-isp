"""Camada OpenAI sem ferramentas: recebe somente o contexto explicitamente fornecido."""

import json
from typing import Any

from app.config import get_settings


def llm_is_configured() -> bool:
    return bool(get_settings().openai_api_key)


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
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    from openai import APIStatusError, OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.responses.create(
            model=settings.openai_model,
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
        raise RuntimeError(f"Falha na OpenAI: {exc.message}") from exc

    if not response.output_text:
        raise RuntimeError("A OpenAI não retornou conteúdo estruturado")
    return json.loads(response.output_text)

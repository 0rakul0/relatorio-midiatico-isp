"""Camada OpenAI sem ferramentas: a LLM recebe somente o contexto explícito."""
import json
from typing import Any

from app.config import get_settings


def structured_response(*, instructions: str, payload: dict[str, Any], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Executa uma resposta estruturada, sem web search ou outras ferramentas."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    from openai import OpenAI

    response = OpenAI(api_key=settings.openai_api_key).responses.create(
        model=settings.openai_model,
        instructions=instructions,
        input=json.dumps(payload, ensure_ascii=False),
        text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
        max_output_tokens=4000,
        store=False,
    )
    if not response.output_text:
        raise RuntimeError("A LLM não retornou conteúdo estruturado")
    return json.loads(response.output_text)

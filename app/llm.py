"""Camada OpenAI sem ferramentas: a LLM recebe somente o contexto explícito."""
import json
from typing import Any

from app.config import get_settings


def structured_response(*, instructions: str, payload: dict[str, Any], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Executa uma resposta estruturada, sem web search ou outras ferramentas."""
    settings = get_settings()
    from openai import APIStatusError, OpenAI

    provider = settings.llm_provider.strip().lower()
    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY não configurada")
        client = OpenAI(api_key=settings.groq_api_key, base_url="https://api.groq.com/openai/v1")
        model = settings.groq_model
    elif provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY não configurada")
        client = OpenAI(api_key=settings.openai_api_key)
        model = settings.openai_model
    else:
        raise RuntimeError("LLM_PROVIDER deve ser 'openai' ou 'groq'")

    try:
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=False),
            text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
            max_output_tokens=4000,
            store=False,
        )
    except APIStatusError as exc:
        raise RuntimeError(f"Falha no provedor {provider}: {exc.message}") from exc
    if not response.output_text:
        raise RuntimeError("A LLM não retornou conteúdo estruturado")
    return json.loads(response.output_text)

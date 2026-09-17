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


def web_search_structured_response(
    *,
    instructions: str,
    payload: dict[str, Any],
    schema_name: str,
    schema: dict[str, Any],
    allowed_domains: list[str] | None = None,
    max_output_tokens: int = 5000,
    model: str | None = None,
    retry_without_domain_filter: bool = False,
    fallback_model: str | None = None,
) -> dict[str, Any]:
    """Executa Web Search e devolve JSON validado.

    Para buscas em sites, o chamador pode habilitar duas protecoes extras:
    1. repetir sem ``allowed_domains`` se o filtro de dominio for rejeitado;
    2. tentar um segundo modelo que ja esteja funcionando para Web Search.

    Isso nao amplia a quantidade normal de chamadas: os retries so acontecem
    quando a tentativa anterior falha antes de produzir um resultado utilizavel.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada")

    from openai import APIStatusError, OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    primary_model = model or settings.web_search_model
    models = [primary_model]
    if fallback_model and fallback_model not in models:
        models.append(fallback_model)

    attempts: list[tuple[str, list[str] | None]] = []
    for candidate_model in models:
        attempts.append((candidate_model, allowed_domains))
        if retry_without_domain_filter and allowed_domains:
            attempts.append((candidate_model, None))

    errors: list[str] = []
    for candidate_model, candidate_domains in attempts:
        try:
            web_search_tool: dict[str, Any] = {"type": "web_search"}
            if candidate_domains:
                web_search_tool["filters"] = {"allowed_domains": candidate_domains}

            response = client.responses.create(
                model=candidate_model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                tools=[web_search_tool],
                tool_choice="required",
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
            domain_note = f" domains={','.join(candidate_domains)}" if candidate_domains else " sem filtro de dominio"
            errors.append(f"{candidate_model}{domain_note}: {exc.message}")
            continue
        except Exception as exc:
            domain_note = f" domains={','.join(candidate_domains)}" if candidate_domains else " sem filtro de dominio"
            errors.append(f"{candidate_model}{domain_note}: {exc}")
            continue

        if not response.output_text:
            errors.append(f"{candidate_model}: resposta sem conteúdo estruturado")
            continue
        try:
            return json.loads(response.output_text)
        except json.JSONDecodeError as exc:
            errors.append(f"{candidate_model}: JSON inválido ({exc})")
            continue

    detail = " | ".join(errors[:4]) or "falha não especificada"
    raise RuntimeError(f"Falha na OpenAI Web Search após as tentativas previstas: {detail}")

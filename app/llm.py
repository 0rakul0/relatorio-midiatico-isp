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
            _record(
                model=candidate_model,
                caller="web_search_structured_response",
                success=False,
                error=exc.message,
                schema_name=schema_name,
            )
            continue
        except Exception as exc:
            domain_note = f" domains={','.join(candidate_domains)}" if candidate_domains else " sem filtro de dominio"
            errors.append(f"{candidate_model}{domain_note}: {exc}")
            _record(
                model=candidate_model,
                caller="web_search_structured_response",
                success=False,
                error=str(exc),
                schema_name=schema_name,
            )
            continue

        counts = _usage_counts(getattr(response, "usage", None))
        counts["search_calls"] = 1
        if not response.output_text:
            errors.append(f"{candidate_model}: resposta sem conteúdo estruturado")
            _record(
                model=candidate_model,
                caller="web_search_structured_response",
                success=False,
                error="resposta sem conteúdo estruturado",
                schema_name=schema_name,
                **counts,
            )
            continue
        try:
            parsed = json.loads(response.output_text)
        except json.JSONDecodeError as exc:
            errors.append(f"{candidate_model}: JSON inválido ({exc})")
            _record(
                model=candidate_model,
                caller="web_search_structured_response",
                success=False,
                error=f"JSON inválido ({exc})",
                schema_name=schema_name,
                **counts,
            )
            continue
        _record(
            model=candidate_model,
            caller="web_search_structured_response",
            success=True,
            schema_name=schema_name,
            **counts,
        )
        return parsed

    detail = " | ".join(errors[:4]) or "falha não especificada"
    raise RuntimeError(f"Falha na OpenAI Web Search após as tentativas previstas: {detail}")

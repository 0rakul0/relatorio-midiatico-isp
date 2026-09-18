"""Adapter do provedor Tavily.

Concentra a chamada ao SDK ``tavily`` e a normalização dos resultados. Não
conhece persistência nem o agente; deve ser importado exclusivamente por
``app.tools.search``.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def is_tavily_hard_failure(exc: Exception | str) -> bool:
    """Falhas que tornam inútil insistir no Tavily durante a mesma execução."""
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code in {401, 402, 403, 429}:
        return True

    message = str(exc).casefold()
    signals = (
        "please upgrade",
        "upgrade your plan",
        "request exceeds your plan",
        "exceeds your plan's set usage limit",
        "set usage limit",
        "usage limit",
        "usage_limit",
        "monthly limit",
        "plan limit",
        "quota",
        "insufficient credits",
        "credit limit",
        "rate limit",
        "rate_limit",
        "too many requests",
        "contact support@tavily.com",
        "limit exceeded",
        "exceeded your",
        "invalid api key",
        "unauthorized",
        "forbidden",
    )
    return any(signal in message for signal in signals)


def tavily_search(
    query: str,
    *,
    max_results: int,
    video_only: bool = False,
    window_start: str | None = None,
    window_end: str | None = None,
    purpose: str = "MEDIA_REPERCUSSION",
) -> list[dict[str, Any]]:
    """Executa uma busca no Tavily e devolve linhas já normalizadas.

    Retorna ``[]`` quando a API não está configurada ou quando a falha é
    transitória; levanta ``RuntimeError`` em falhas que inviabilizam novas
    tentativas na mesma execução (cota, chave inválida, rate limit).
    """
    settings = get_settings()
    if not settings.tavily_api_key:
        return []

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=settings.tavily_api_key)
        actual_query = query
        if video_only and "site:youtube.com" not in query.lower() and "site:youtu.be" not in query.lower():
            actual_query = f"site:youtube.com {query}"
        params: dict[str, Any] = {
            "query": actual_query,
            "max_results": max_results,
            "include_raw_content": "text",
            "topic": "general" if video_only else ("news" if purpose == "MEDIA_REPERCUSSION" else "general"),
            "search_depth": "advanced" if video_only or purpose != "MEDIA_REPERCUSSION" else "basic",
            "timeout": 15,
        }
        if window_start and window_end:
            params["start_date"] = window_start
            params["end_date"] = window_end
        response = client.search(**params)
    except Exception as exc:
        message = str(exc).casefold()
        date_error = window_start and window_end and any(
            token in message
            for token in ("start_date", "end_date", "date cannot", "date must", "same")
        )
        if not date_error:
            if is_tavily_hard_failure(exc):
                raise RuntimeError(str(exc)) from exc
            return []
        try:
            retry_params = {key: value for key, value in params.items() if key not in {"start_date", "end_date"}}
            response = client.search(**retry_params)
        except Exception:
            return []

    if isinstance(response, dict) and response.get("error"):
        raise RuntimeError(str(response.get("error")))

    rows: list[dict[str, Any]] = []
    for item in response.get("results", []) or []:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        if video_only and not ("youtube.com" in url.lower() or "youtu.be" in url.lower()):
            continue
        rows.append(
            {
                "title": str(item.get("title") or ("Video sem titulo" if video_only else "Sem titulo")),
                "url": url,
                "snippet": item.get("content"),
                "content": item.get("raw_content") or item.get("content"),
                "published_at": item.get("published_date"),
                "source_name": "YouTube" if video_only else None,
                "provider": "tavily",
            }
        )
    return rows

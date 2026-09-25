"""Cliente REST minimo para Actors sociais do Apify."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ApifyUnavailable(RuntimeError):
    """Falha de configuracao, rede ou execucao do Actor."""


def run_actor_dataset(
    *,
    actor_id: str,
    token: str,
    payload: dict,
    base_url: str = "https://api.apify.com/v2",
    timeout_seconds: int = 240,
    max_items: int | None = None,
) -> list[dict]:
    actor = quote(str(actor_id or "").strip().replace("/", "~"), safe="~")
    if not actor:
        raise ApifyUnavailable("Actor do Apify nao configurado")
    if not str(token or "").strip():
        raise ApifyUnavailable("APIFY_API_TOKEN nao configurado")

    url = (
        f"{base_url.rstrip('/')}/actors/{actor}/run-sync-get-dataset-items"
        "?format=json&clean=true"
    )
    if max_items is not None:
        url += f"&maxItems={max(1, int(max_items))}"
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "relatorio-midiatico-isp/0.4",
        },
        method="POST",
    )
    try:
        with urlopen(
            request,
            timeout=max(30, min(int(timeout_seconds), 295)),
        ) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
        except Exception:
            detail = str(exc)
        raise ApifyUnavailable(
            f"Apify HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ApifyUnavailable(f"Apify indisponivel: {exc}") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ApifyUnavailable("Resposta invalida do Apify") from exc

    if not isinstance(data, list):
        raise ApifyUnavailable("O Actor nao retornou uma lista de itens")
    return [dict(item) for item in data if isinstance(item, dict)]

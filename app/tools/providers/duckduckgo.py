"""Adapter do provedor DuckDuckGo (texto, notícias e vídeos).

Fala apenas com o pacote externo ``ddgs``/``duckduckgo_search`` e não conhece
configuração de aplicação, esquemas nem persistência. Deve ser importado
exclusivamente por ``app.tools.search``.
"""

from __future__ import annotations

import ipaddress
import random
import socket
import time
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DuckDuckGoUnavailable(RuntimeError):
    """Indica que o pacote DDGS nao esta instalado ou que a busca falhou."""


def _load_ddgs_class():
    """Aceita tanto o pacote novo ``ddgs`` quanto o legado ``duckduckgo_search``."""
    try:
        from ddgs import DDGS  # type: ignore

        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore

            return DDGS
        except ImportError as exc:
            raise DuckDuckGoUnavailable(
                "DuckDuckGo nao instalado. Execute `uv add ddgs` "
                "(ou, para compatibilidade, `uv add duckduckgo-search`)."
            ) from exc


def duckduckgo_available() -> bool:
    try:
        _load_ddgs_class()
        return True
    except DuckDuckGoUnavailable:
        return False


def _invoke_with_query_alias(method: Callable[..., Any], query: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Compatibiliza a assinatura antiga ``keywords=`` com a nova ``query=``."""
    try:
        result = method(keywords=query, **kwargs)
    except TypeError:
        result = method(query=query, **kwargs)
    return list(result or [])


def _with_retries(operation: Callable[[], list[dict[str, Any]]], *, retries: int, base_delay: float) -> list[dict[str, Any]]:
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            return operation()
        except Exception as exc:  # biblioteca pode expor classes diferentes entre versoes
            last_error = exc
            if attempt >= retries:
                break
            delay = max(0.1, base_delay) * (2**attempt) + random.uniform(0.0, 0.35)
            time.sleep(delay)
    raise DuckDuckGoUnavailable(f"Falha na busca DuckDuckGo: {last_error}") from last_error


def _client():
    DDGS = _load_ddgs_class()
    return DDGS()


def search_text(
    query: str,
    *,
    max_results: int = 5,
    region: str = "br-pt",
    safesearch: str = "moderate",
    retries: int = 2,
    retry_base_seconds: float = 0.8,
) -> list[dict[str, Any]]:
    def run() -> list[dict[str, Any]]:
        with _client() as ddgs:
            rows = _invoke_with_query_alias(
                ddgs.text,
                query,
                region=region,
                safesearch=safesearch,
                max_results=max(1, max_results),
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            url = str(row.get("href") or row.get("url") or "").strip()
            if not url:
                continue
            normalized.append(
                {
                    "title": str(row.get("title") or "Sem titulo"),
                    "url": url,
                    "snippet": row.get("body") or row.get("description"),
                    "published_at": row.get("date") or row.get("published"),
                    "source_name": row.get("source") or urlparse(url).netloc.lower(),
                    "provider": "duckduckgo_text",
                    "raw": row,
                }
            )
        return normalized

    return _with_retries(run, retries=retries, base_delay=retry_base_seconds)


def search_news(
    query: str,
    *,
    max_results: int = 5,
    region: str = "br-pt",
    safesearch: str = "moderate",
    retries: int = 2,
    retry_base_seconds: float = 0.8,
) -> list[dict[str, Any]]:
    def run() -> list[dict[str, Any]]:
        with _client() as ddgs:
            rows = _invoke_with_query_alias(
                ddgs.news,
                query,
                region=region,
                safesearch=safesearch,
                max_results=max(1, max_results),
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            url = str(row.get("url") or row.get("href") or "").strip()
            if not url:
                continue
            normalized.append(
                {
                    "title": str(row.get("title") or "Sem titulo"),
                    "url": url,
                    "snippet": row.get("body") or row.get("description"),
                    "published_at": row.get("date") or row.get("published"),
                    "source_name": row.get("source") or urlparse(url).netloc.lower(),
                    "provider": "duckduckgo_news",
                    "raw": row,
                }
            )
        return normalized

    return _with_retries(run, retries=retries, base_delay=retry_base_seconds)


def search_videos(
    query: str,
    *,
    max_results: int = 5,
    region: str = "br-pt",
    safesearch: str = "moderate",
    retries: int = 2,
    retry_base_seconds: float = 0.8,
) -> list[dict[str, Any]]:
    def run() -> list[dict[str, Any]]:
        with _client() as ddgs:
            rows = _invoke_with_query_alias(
                ddgs.videos,
                query,
                region=region,
                safesearch=safesearch,
                max_results=max(1, max_results),
            )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            url = str(
                row.get("content")
                or row.get("url")
                or row.get("href")
                or row.get("embed_url")
                or ""
            ).strip()
            if not url:
                continue
            statistics = row.get("statistics") if isinstance(row.get("statistics"), dict) else {}
            raw_views = statistics.get("viewCount") if statistics else row.get("view_count")
            try:
                view_count = int(raw_views) if raw_views not in (None, "") else None
            except (TypeError, ValueError):
                view_count = None

            normalized.append(
                {
                    "title": str(row.get("title") or "Video sem titulo"),
                    "url": url,
                    "description": row.get("description"),
                    "published_at": row.get("published") or row.get("date"),
                    "channel": row.get("uploader") or row.get("creator") or row.get("publisher"),
                    "view_count": view_count,
                    "duration": row.get("duration"),
                    "provider": "duckduckgo_video",
                    "raw": row,
                }
            )
        return normalized

    return _with_retries(run, retries=retries, base_delay=retry_base_seconds)


_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
}


def _host_is_public(host: str | None) -> bool:
    """True somente para hostnames cujos IPs resolvidos são globais.

    Bloqueia localhost, loopback, faixas privadas, link-local, reservadas,
    multicast e endereços não especificados (proteção SSRF).
    """
    if not host:
        return False
    candidate = host.strip().strip("[]").lower().rstrip(".")
    if not candidate or candidate in _BLOCKED_HOSTNAMES:
        return False

    try:
        return ipaddress.ip_address(candidate).is_global
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(candidate, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        address = info[4][0]
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def _url_is_public(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return _host_is_public(parsed.hostname)


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Revalida cada salto de redirecionamento antes de segui-lo."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urljoin(req.full_url, newurl)
        if not _url_is_public(target):
            raise HTTPError(
                req.full_url,
                code,
                f"redirecionamento bloqueado para destino não público: {target}",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, target)


class _VisibleTextParser(HTMLParser):
    _skip_tags = {"script", "style", "noscript", "svg", "canvas", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._skip_tags and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def fetch_url_text(
    url: str,
    *,
    max_chars: int = 12000,
    timeout: float = 10.0,
) -> str | None:
    """Baixa HTML simples para enriquecer o snippet; falhas nao interrompem a coleta."""
    if not _url_is_public(url):
        return None

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.6",
        },
    )
    try:
        with build_opener(_SafeRedirectHandler()).open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                return None
            raw = response.read(512_000)
            charset = response.headers.get_content_charset() or "utf-8"
    except Exception:
        return None

    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")

    parser = _VisibleTextParser()
    try:
        parser.feed(html)
    except Exception:
        return None

    text = "\n".join(parser.parts)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[: max(500, max_chars)] or None

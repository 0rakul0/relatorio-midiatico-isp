"""Provedores publicos de literatura cientifica, normalizados para o pipeline."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


class AcademicProviderUnavailable(RuntimeError):
    pass


def _get_json(url: str, *, timeout: int = 20) -> dict:
    req = Request(url, headers={
        "User-Agent": "relatorio-midiatico-isp/0.5 academic-research (mailto:isp@isp.rj.gov.br)",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise AcademicProviderUnavailable(str(exc)) from exc


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split()).strip()
    return value or None


def _doi(value: Any) -> str | None:
    value = _text(value)
    if not value:
        return None
    return value.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()


def _abstract_from_openalex(index: dict | None) -> str | None:
    if not index:
        return None
    positions: list[tuple[int, str]] = []
    for token, indexes in index.items():
        for position in indexes or []:
            positions.append((int(position), str(token)))
    positions.sort()
    return " ".join(token for _, token in positions) or None


class _ScieloResultsParser(HTMLParser):
    """Extrai apenas links de artigos SciELO presentes no HTML de resultados."""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._url: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        full = urljoin("https://search.scielo.org/", href)
        parsed = urlparse(full)
        if parsed.hostname in {"www.scielo.br", "scielo.br"} and "/j/" in parsed.path and "/a/" in parsed.path:
            self._url = full
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._url:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._url:
            title = _text(" ".join(self._text))
            if title:
                self.links.append((self._url, title))
            self._url = None
            self._text = []


def search_scielo(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    """Pesquisa diretamente no indice publico SciELO, sem inventar metadados.

    Em indisponibilidade/alteracao do portal, o chamador pode recorrer ao
    Crossref filtrado pelo prefixo DOI 10.1590.
    """
    params = urlencode({"q": query, "lang": "pt", "count": max(1, max_results)})
    url = f"https://search.scielo.org/?{params}"
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; relatorio-midiatico-isp/0.5)",
        "Accept": "text/html",
    })
    try:
        with urlopen(request, timeout=15) as response:
            html = response.read(900_000).decode("utf-8", errors="replace")
    except Exception as exc:
        raise AcademicProviderUnavailable(f"Pesquisa direta SciELO indisponivel: {exc}") from exc
    parser = _ScieloResultsParser()
    parser.feed(html)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link, title in parser.links:
        canonical = link.split("?")[0].rstrip("/")
        if canonical in seen:
            continue
        seen.add(canonical)
        rows.append({
            "provider": "scielo", "external_id": canonical,
            "arxiv_id": None, "doi": None, "title": title,
            "authors": [], "abstract": None, "published_at": None,
            "updated_at": None, "categories": [], "url": link,
            "pdf_url": None, "journal_reference": None, "is_preprint": False,
        })
        if len(rows) >= max_results:
            break
    return rows


def search_openalex(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    params = urlencode({"search": query, "per-page": max_results})
    data = _get_json(f"https://api.openalex.org/works?{params}")
    rows = []
    for item in data.get("results") or []:
        oid = str(item.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        if not oid:
            continue
        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        rows.append({
            "provider": "openalex", "external_id": oid, "arxiv_id": None,
            "doi": _doi(item.get("doi")), "title": _text(item.get("display_name")) or "Sem titulo",
            "authors": [a.get("author", {}).get("display_name") for a in item.get("authorships") or [] if a.get("author", {}).get("display_name")],
            "abstract": _abstract_from_openalex(item.get("abstract_inverted_index")),
            "published_at": item.get("publication_date"), "updated_at": None,
            "categories": [c.get("display_name") for c in item.get("topics") or [] if c.get("display_name")][:20],
            "url": item.get("doi") or item.get("id"), "pdf_url": (location.get("pdf_url") or None),
            "journal_reference": source.get("display_name"), "is_preprint": str(item.get("type") or "").lower() in {"preprint", "posted-content"},
        })
    return rows


def search_crossref(query: str, *, max_results: int = 5, scielo_only: bool = False) -> list[dict[str, Any]]:
    base = "https://api.crossref.org/prefixes/10.1590/works" if scielo_only else "https://api.crossref.org/works"
    params = urlencode({"query.bibliographic": query, "rows": max_results, "select": "DOI,title,author,abstract,published,URL,container-title,type"})
    data = _get_json(f"{base}?{params}")
    rows = []
    for item in (data.get("message") or {}).get("items") or []:
        doi = _doi(item.get("DOI"))
        if not doi:
            continue
        parts = (((item.get("published") or {}).get("date-parts") or [[None]])[0])
        published = "-".join(str(x).zfill(2) for x in parts if x is not None) or None
        authors = []
        for author in item.get("author") or []:
            name = " ".join(x for x in [author.get("given"), author.get("family")] if x)
            if name: authors.append(name)
        provider = "scielo" if scielo_only else "crossref"
        rows.append({
            "provider": provider, "external_id": doi, "arxiv_id": None, "doi": doi,
            "title": _text((item.get("title") or [""])[0]) or "Sem titulo", "authors": authors,
            "abstract": _text(item.get("abstract")), "published_at": published, "updated_at": None,
            "categories": [], "url": item.get("URL") or f"https://doi.org/{doi}", "pdf_url": None,
            "journal_reference": _text((item.get("container-title") or [""])[0]),
            "is_preprint": str(item.get("type") or "").lower() in {"posted-content", "preprint"},
        })
    return rows


def search_semantic_scholar(query: str, *, max_results: int = 5) -> list[dict[str, Any]]:
    fields = "paperId,title,abstract,authors,year,publicationDate,url,openAccessPdf,externalIds,venue"
    params = urlencode({"query": query, "limit": max_results, "fields": fields})
    data = _get_json(f"https://api.semanticscholar.org/graph/v1/paper/search?{params}")
    rows = []
    for item in data.get("data") or []:
        pid = _text(item.get("paperId"))
        if not pid: continue
        ext = item.get("externalIds") or {}
        pdf = item.get("openAccessPdf") or {}
        rows.append({
            "provider": "semantic_scholar", "external_id": pid, "arxiv_id": ext.get("ArXiv"),
            "doi": _doi(ext.get("DOI")), "title": _text(item.get("title")) or "Sem titulo",
            "authors": [a.get("name") for a in item.get("authors") or [] if a.get("name")],
            "abstract": _text(item.get("abstract")), "published_at": item.get("publicationDate") or (f"{item.get('year')}-01-01" if item.get("year") else None),
            "updated_at": None, "categories": [], "url": item.get("url"), "pdf_url": pdf.get("url"),
            "journal_reference": _text(item.get("venue")), "is_preprint": False,
        })
    return rows

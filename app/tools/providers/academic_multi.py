"""Provedores publicos de literatura cientifica, normalizados para o pipeline."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode
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

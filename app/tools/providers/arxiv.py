"""Adaptador da API publica do arXiv.

Este modulo e infraestrutura: conhece HTTP/Atom, mas nao decide quando pesquisar
nem se um artigo e relevante para a pauta. Essa decisao pertence ao ReportAgent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


ARXIV_API_URL = "https://export.arxiv.org/api/query"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"


class ArxivUnavailable(RuntimeError):
    """Falha de acesso ou resposta invalida do arXiv."""


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    compact = " ".join(value.split()).strip()
    return compact or None


def _normalize_query(query: str) -> str:
    compact = " ".join(str(query or "").split()).strip()
    if not compact:
        raise ValueError("Consulta academica vazia")

    # Permite que o agente use a sintaxe oficial do arXiv quando quiser
    # (all:, ti:, abs:, au:, cat:). Caso contrario, fazemos uma busca ampla.
    lowered = compact.casefold()
    if any(token in lowered for token in ("all:", "ti:", "abs:", "au:", "cat:")):
        return compact

    safe = compact.replace('"', " ").strip()
    return f'all:"{safe}"'


def _iso_date(value: str | None) -> str | None:
    value = _clean_text(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else value


def search_arxiv(query: str, *, max_results: int = 6, timeout: int = 25) -> list[dict[str, Any]]:
    """Pesquisa metadados no arXiv e retorna uma lista normalizada."""

    limit = max(1, min(int(max_results or 6), 10))
    search_query = _normalize_query(query)
    params = urlencode(
        {
            "search_query": search_query,
            "start": 0,
            "max_results": limit,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )
    request = Request(
        f"{ARXIV_API_URL}?{params}",
        headers={
            "User-Agent": "relatorio-midiatico-isp/0.4 academic-research",
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.1",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except Exception as exc:
        raise ArxivUnavailable(f"Falha ao consultar arXiv: {exc}") from exc

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ArxivUnavailable(f"Resposta Atom invalida do arXiv: {exc}") from exc

    ns = {"atom": _ATOM_NS, "arxiv": _ARXIV_NS}
    results: list[dict[str, Any]] = []

    for entry in root.findall("atom:entry", ns):
        abs_url = _clean_text(entry.findtext("atom:id", default="", namespaces=ns))
        if not abs_url:
            continue

        external_id = abs_url.rstrip("/").rsplit("/", 1)[-1]
        authors = [
            name
            for author in entry.findall("atom:author", ns)
            if (name := _clean_text(author.findtext("atom:name", default="", namespaces=ns)))
        ]
        categories = [
            str(category.attrib.get("term") or "").strip()
            for category in entry.findall("atom:category", ns)
            if str(category.attrib.get("term") or "").strip()
        ]

        pdf_url = None
        for link in entry.findall("atom:link", ns):
            href = str(link.attrib.get("href") or "").strip()
            link_type = str(link.attrib.get("type") or "").casefold()
            title = str(link.attrib.get("title") or "").casefold()
            if href and ("pdf" in link_type or title == "pdf"):
                pdf_url = href
                break

        doi = _clean_text(entry.findtext("arxiv:doi", default="", namespaces=ns))
        journal_reference = _clean_text(
            entry.findtext("arxiv:journal_ref", default="", namespaces=ns)
        )

        results.append(
            {
                "provider": "arxiv",
                "external_id": external_id,
                "arxiv_id": external_id,
                "doi": doi,
                "title": _clean_text(entry.findtext("atom:title", default="", namespaces=ns))
                or "Sem titulo",
                "authors": authors,
                "abstract": _clean_text(entry.findtext("atom:summary", default="", namespaces=ns)),
                "published_at": _iso_date(
                    entry.findtext("atom:published", default="", namespaces=ns)
                ),
                "updated_at": _iso_date(
                    entry.findtext("atom:updated", default="", namespaces=ns)
                ),
                "categories": categories,
                "url": abs_url,
                "pdf_url": pdf_url,
                "journal_reference": journal_reference,
                # arXiv e um repositorio de e-prints; a existencia de journal_ref
                # nao permite assumir revisao por pares para todos os registros.
                "is_preprint": journal_reference is None,
            }
        )

    return results

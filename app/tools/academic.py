"""Tool multi-base de literatura cientifica oferecida ao ReportAgent."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from langchain_core.tools import StructuredTool

from app.schemas import AgentAcademicSearchArgs, AcademicSearchToolResponse
from app.tools.providers.arxiv import ArxivUnavailable, search_arxiv
from app.tools.providers.academic_multi import (
    AcademicProviderUnavailable,
    search_crossref,
    search_openalex,
    search_semantic_scholar,
)

AcademicSink = Callable[[list[dict[str, Any]], str], None]


def _dedup_key(row: dict) -> str:
    doi = str(row.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    title = " ".join(str(row.get("title") or "").casefold().split())
    return "title:" + title


def make_academic_search_tool(*, sink: AcademicSink | None = None) -> StructuredTool:
    """Pesquisa SciELO, OpenAlex, Crossref, Semantic Scholar e arXiv."""

    def pesquisar_literatura_cientifica(
        queries: list[str],
        max_results_per_query: int = 6,
    ) -> dict[str, Any]:
        clean_queries = [" ".join(str(q or "").split()).strip() for q in queries or []]
        clean_queries = [q for q in clean_queries if q][:3]
        limit = max(1, min(int(max_results_per_query or 6), 8))
        combined: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()

        # Consultas independentes rodam em paralelo. SciELO usa o prefixo DOI
        # 10.1590 no Crossref, cobrindo grande parte da colecao brasileira sem
        # scraping do portal.
        jobs = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            for query in clean_queries:
                jobs.extend([
                    ("scielo", query, pool.submit(search_crossref, query, max_results=limit, scielo_only=True)),
                    ("openalex", query, pool.submit(search_openalex, query, max_results=limit)),
                    ("crossref", query, pool.submit(search_crossref, query, max_results=limit)),
                    ("semantic_scholar", query, pool.submit(search_semantic_scholar, query, max_results=limit)),
                    ("arxiv", query, pool.submit(search_arxiv, query, max_results=limit)),
                ])
            for provider, query, future in jobs:
                try:
                    rows = future.result()
                except (AcademicProviderUnavailable, ArxivUnavailable, Exception) as exc:
                    errors.append(f"{provider} | {query}: {exc}")
                    continue
                if sink is not None and rows:
                    sink(rows, provider)
                for row in rows:
                    key = _dedup_key(row)
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    combined.append(row)

        # Prioriza SciELO e bases com metadados estruturados antes do arXiv
        # para temas brasileiros; relevancia final continua a cargo do agente.
        priority = {"scielo": 0, "openalex": 1, "crossref": 2, "semantic_scholar": 3, "arxiv": 4}
        combined.sort(key=lambda row: priority.get(str(row.get("provider")), 9))
        payload = {
            "queries": clean_queries,
            "provider": "multi",
            "status": "OK" if combined else ("ERROR" if errors else "NO_RESULTS"),
            "results": combined[:20],
            "errors": errors[:10],
        }
        return AcademicSearchToolResponse.model_validate(payload).model_dump(mode="json")

    return StructuredTool.from_function(
        func=pesquisar_literatura_cientifica,
        args_schema=AgentAcademicSearchArgs,
        name="pesquisar_literatura_cientifica",
        description=(
            "Pesquisa literatura cientifica em SciELO, OpenAlex, Crossref, "
            "Semantic Scholar e arXiv. Use consultas conceituais em portugues "
            "e ingles; a literatura nao precisa estar limitada ao periodo das noticias."
        ),
        return_direct=False,
    )

"""Tools academicas oferecidas ao ReportAgent."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from app.schemas import AgentAcademicSearchArgs, AcademicSearchToolResponse
from app.tools.providers.arxiv import ArxivUnavailable, search_arxiv


AcademicSink = Callable[[list[dict[str, Any]], str], None]


def make_academic_search_tool(*, sink: AcademicSink | None = None) -> StructuredTool:
    """Cria a tool de literatura cientifica.

    O agente escolhe as consultas; a tool apenas executa o plano no arXiv.
    O sink permite ao service capturar deterministicamente os resultados reais
    e rejeitar qualquer referencia que nao tenha vindo da ferramenta.
    """

    def pesquisar_artigos_arxiv(
        queries: list[str],
        max_results_per_query: int = 6,
    ) -> dict[str, Any]:
        clean_queries = [
            " ".join(str(query or "").split()).strip()
            for query in list(queries or [])
            if " ".join(str(query or "").split()).strip()
        ][:3]
        limit = max(1, min(int(max_results_per_query or 6), 10))

        combined: list[dict[str, Any]] = []
        seen: set[str] = set()
        errors: list[str] = []

        for index, query in enumerate(clean_queries):
            # Recomendacao operacional da API do arXiv: evite chamadas em rajada.
            if index:
                time.sleep(3.0)
            try:
                rows = search_arxiv(query, max_results=limit)
            except ArxivUnavailable as exc:
                errors.append(f"{query}: {exc}")
                continue

            for row in rows:
                key = str(row.get("external_id") or row.get("url") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                combined.append(row)

        if sink is not None and combined:
            sink(combined, "arxiv")

        status = "OK" if combined else ("ERROR" if errors else "NO_RESULTS")
        payload = {
            "queries": clean_queries,
            "provider": "arxiv",
            "status": status,
            "results": combined[:20],
            "errors": errors[:10],
        }
        return AcademicSearchToolResponse.model_validate(payload).model_dump(mode="json")

    return StructuredTool.from_function(
        func=pesquisar_artigos_arxiv,
        args_schema=AgentAcademicSearchArgs,
        name="pesquisar_artigos_arxiv",
        description=(
            "Pesquisa literatura cientifica relacionada ao tema no arXiv. "
            "Aceita de uma a tres consultas e retorna apenas metadados/abstracts reais."
        ),
        return_direct=False,
    )

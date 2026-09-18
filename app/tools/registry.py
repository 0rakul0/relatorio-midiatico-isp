"""Registro central das tools oferecidas ao ReportAgent.

Adicionar uma nova capacidade externa deve exigir alteração principalmente neste
pacote, não no motor do agente. O caller escolhe quais capacidades disponibilizar
em cada tarefa; a LLM decide se chama alguma das tools efetivamente oferecidas.

``bulk=True`` substitui as tools individuais pelas versões em lote, usadas pela
coleta obrigatória para executar todo o plano em poucas chamadas do agente.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tools.hydration import ArticleHydrationSink, make_bulk_article_fetch_tool
from app.tools.search import (
    SearchContextResolver,
    SearchObserver,
    SearchSink,
    make_bulk_video_search_tool,
    make_bulk_web_search_tool,
    make_video_search_tool,
    make_web_search_tool,
)


def build_agent_tools(
    *,
    enable_web: bool = False,
    enable_video: bool = False,
    enable_article_fetch: bool = False,
    web_sink: SearchSink | None = None,
    video_sink: SearchSink | None = None,
    article_sink: ArticleHydrationSink | None = None,
    web_context: SearchContextResolver | None = None,
    video_context: SearchContextResolver | None = None,
    web_observer: SearchObserver | None = None,
    video_observer: SearchObserver | None = None,
    web_providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    video_providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    bulk: bool = False,
) -> list[BaseTool]:
    tools: list[BaseTool] = []
    if enable_web:
        web_factory = make_bulk_web_search_tool if bulk else make_web_search_tool
        tools.append(
            web_factory(
                sink=web_sink,
                context=web_context,
                providers=web_providers,
                observer=web_observer,
            )
        )
    if enable_video:
        video_factory = make_bulk_video_search_tool if bulk else make_video_search_tool
        tools.append(
            video_factory(
                sink=video_sink,
                context=video_context,
                providers=video_providers,
                observer=video_observer,
            )
        )
    if enable_article_fetch:
        tools.append(make_bulk_article_fetch_tool(sink=article_sink))
    return tools

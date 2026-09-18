"""Registro central das tools oferecidas ao ReportAgent.

Adicionar uma nova capacidade externa deve exigir alteração principalmente neste
pacote, não no motor do agente. O caller escolhe quais capacidades disponibilizar
em cada tarefa; a LLM decide se chama alguma das tools efetivamente oferecidas.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.tools.search import (
    SearchAttempt,
    SearchContextResolver,
    SearchSink,
    make_video_search_tool,
    make_web_search_tool,
)


def build_agent_tools(
    *,
    enable_web: bool = False,
    enable_video: bool = False,
    web_sink: SearchSink | None = None,
    video_sink: SearchSink | None = None,
    web_context: SearchContextResolver | None = None,
    video_context: SearchContextResolver | None = None,
    web_on_attempt: SearchAttempt | None = None,
    video_on_attempt: SearchAttempt | None = None,
    web_providers: tuple[str, ...] = ("duckduckgo", "tavily"),
    video_providers: tuple[str, ...] = ("duckduckgo", "tavily"),
) -> list[BaseTool]:
    tools: list[BaseTool] = []
    if enable_web:
        tools.append(
            make_web_search_tool(
                sink=web_sink,
                context=web_context,
                providers=web_providers,
                on_attempt=web_on_attempt,
            )
        )
    if enable_video:
        tools.append(
            make_video_search_tool(
                sink=video_sink,
                context=video_context,
                providers=video_providers,
                on_attempt=video_on_attempt,
            )
        )
    return tools

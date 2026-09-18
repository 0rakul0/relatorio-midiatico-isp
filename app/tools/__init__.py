"""Tools opcionais disponíveis para o agente único."""
from app.tools.registry import build_agent_tools
from app.tools.search import (
    SearchContextResolver,
    SearchObserver,
    SearchSink,
    make_bulk_video_search_tool,
    make_bulk_web_search_tool,
    make_video_search_tool,
    make_web_search_tool,
)

__all__ = [
    "SearchContextResolver",
    "SearchObserver",
    "SearchSink",
    "build_agent_tools",
    "make_bulk_video_search_tool",
    "make_bulk_web_search_tool",
    "make_video_search_tool",
    "make_web_search_tool",
]

"""Serviços da metodologia, separados por responsabilidade.

Este pacote substitui o antigo ``app/services.py`` monolítico. Os nomes públicos
mais usados continuam reexportados aqui para preservar compatibilidade com
``from app.services import ...``.
"""

from .cache import cached_report_for_project, cached_report_for_topic
from .classification import classify_with_llm
from .collection.common import canonicalize, query_window
from .collection.web import collect_web
from .collection.youtube import collect_media_sources
from .collection.youtube_helpers import is_youtube_url, youtube_tasks_for_execution
from .corpus_chat import chat_with_all_corpus, chat_with_corpus, list_chat_projects
from .execution_profile import effective_execution_profile, execution_flags
from .metrics import metrics
from .pipeline import run_full_methodology
from .project_profile import discover_project_profile
from .reporting import draft_report_with_llm, export_report_pdf
from .search_planning import plan_queries, plan_queries_with_llm
from .validation import validate_and_classify, validate_video_metadata_cross_source

__all__ = [
    "cached_report_for_project",
    "cached_report_for_topic",
    "canonicalize",
    "chat_with_all_corpus",
    "chat_with_corpus",
    "classify_with_llm",
    "collect_media_sources",
    "collect_web",
    "discover_project_profile",
    "draft_report_with_llm",
    "effective_execution_profile",
    "execution_flags",
    "export_report_pdf",
    "is_youtube_url",
    "list_chat_projects",
    "metrics",
    "plan_queries",
    "plan_queries_with_llm",
    "query_window",
    "run_full_methodology",
    "validate_and_classify",
    "validate_video_metadata_cross_source",
    "youtube_tasks_for_execution",
]

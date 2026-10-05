"""Fachada compatível dos modelos SQLAlchemy.

As definições foram separadas por domínio em app.model_groups. Este módulo
continua reexportando os mesmos nomes para preservar todos os imports existentes.
"""

from app.model_groups.core import AcademicPaper, OfficialFact, Project
from app.model_groups.search import SearchCall, SearchHit, SearchQuery
from app.model_groups.corpus import (
    CorpusDocument,
    MediaItem,
    ProjectCorpusLink,
    RelevanceTrainingExample,
)
from app.model_groups.social import (
    PublicOpinionSurvey,
    SocialAnalysis,
    SocialComment,
    SocialPost,
)
from app.model_groups.facts import (
    Classification,
    FactAssertion,
    FactEvent,
    OperationEvent,
    OperationMediaLink,
)
from app.model_groups.reports import GeneratedReport, ReportRun, ReportVersion
from app.model_groups.users import (
    AppUser,
    ChatConversation,
    ChatMessage,
    LLMCall,
    Subscription,
)

__all__ = [
    "Project",
    "OfficialFact",
    "SearchQuery",
    "SearchCall",
    "SearchHit",
    "CorpusDocument",
    "ProjectCorpusLink",
    "RelevanceTrainingExample",
    "MediaItem",
    "AcademicPaper",
    "SocialPost",
    "SocialComment",
    "SocialAnalysis",
    "PublicOpinionSurvey",
    "FactEvent",
    "FactAssertion",
    "OperationEvent",
    "OperationMediaLink",
    "Classification",
    "GeneratedReport",
    "ReportVersion",
    "ReportRun",
    "AppUser",
    "ChatConversation",
    "ChatMessage",
    "Subscription",
    "LLMCall",
]

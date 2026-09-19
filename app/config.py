from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao"

    # Pool do SQLAlchemy. Banco gerenciado (ex.: Supabase) limita conexões;
    # 5 + 10 comporta o app com alguns runs paralelos. SQLite ignora.
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1, le=300)

    # OpenAI is used only as the LLM. External search is DuckDuckGo only.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    youtube_search_max_results: int = 15

    # Optional agent-tool rounds. Collector normally uses one bulk call per medium.
    max_agent_tool_rounds: int = Field(default=3, ge=1, le=10)

    # Provider result ceiling. Keep this >= max_results_per_query, otherwise the
    # provider layer would silently cap the collection below the requested value.
    agent_search_max_results: int = Field(default=8, ge=1, le=10)

    app_env: str = "development"

    # ------------------------------------------------------------------
    # Search strategy
    # ------------------------------------------------------------------
    # The planner optimizes one primary query and may add only a very small
    # number of materially different complementary queries.
    max_complementary_queries: int = Field(default=2, ge=0, le=4)

    # Media plan = 1 primary + up to 2 complementary + priority portals.
    # This is a safety ceiling, not a target that the planner should fill.
    max_media_queries: int = Field(default=12, ge=1, le=30)
    max_fact_queries: int = Field(default=1, ge=0, le=5)
    max_official_queries: int = Field(default=3, ge=0, le=10)
    max_nominal_queries: int = Field(default=12, ge=0, le=50)

    # Post-validation corpus target. It is NEVER used to stop collection.
    # Search providers execute every approved query and every returned hit is
    # preserved before later validation decides what belongs to the corpus.
    target_media_items: int = Field(default=27, ge=1, le=200)

    # Hard cap on NEW items consolidated from search per project. Every
    # provider hit is still preserved as SearchHit for audit, but once the
    # budget is exhausted no new MediaItem is created, so nothing else flows
    # into the LLM stages (validation, facts, classification). Items reused
    # from the historical corpus (corpus_origin == "REUSED") never consume
    # this budget.
    max_new_media_items: int = Field(default=40, ge=1, le=500)

    # Compatibility/safety statistic. Raw collection is bounded primarily by
    # query count x per-query limits and no longer discards hits at this value.
    max_search_results: int = Field(default=250, ge=1, le=2000)

    # Provider result windows. Priority portal queries preserve the same amount
    # of returned evidence instead of truncating to one or two early hits.
    max_results_per_query: int = Field(default=8, ge=1, le=10)
    max_priority_results_per_query: int = Field(default=8, ge=1, le=10)

    # Separate result budgets prevent factual/official research from consuming
    # the media corpus budget.
    max_fact_search_results: int = Field(default=12, ge=1, le=100)
    max_official_search_results: int = Field(default=12, ge=1, le=100)
    max_nominal_search_results: int = Field(default=24, ge=1, le=200)

    # Backward-compatibility only. New planning code does not use a single
    # shared query budget anymore.
    max_search_queries: int = Field(default=30, ge=1, le=100)

    duckduckgo_region: str = "br-pt"
    duckduckgo_safesearch: str = "moderate"
    duckduckgo_max_retries: int = Field(default=2, ge=0, le=5)
    duckduckgo_retry_base_seconds: float = Field(default=0.8, ge=0.0, le=10.0)
    duckduckgo_fetch_pages: bool = False
    duckduckgo_fetch_max_chars: int = Field(default=12000, ge=1000, le=100000)
    duckduckgo_fetch_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)

    # Full article hydration happens after collection, during news validation.
    article_fetch_enabled: bool = True
    article_fetch_max_items: int = Field(default=80, ge=1, le=500)
    article_fetch_max_chars: int = Field(default=12000, ge=1000, le=100000)
    article_fetch_timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    article_fetch_workers: int = Field(default=5, ge=1, le=12)
    article_fetch_min_existing_chars: int = Field(default=800, ge=0, le=10000)

    # Reuse the historical corpus before opening new searches. Documents are
    # revalidated for each new project, so old relevance decisions are not
    # silently inherited.
    enable_corpus_reuse: bool = True
    corpus_backfill_limit: int = Field(default=20000, ge=100, le=200000)
    corpus_reuse_project_limit: int = Field(default=100, ge=1, le=1000)
    corpus_reuse_max_candidates: int = Field(default=120, ge=1, le=1000)
    corpus_reuse_min_project_score: float = Field(default=0.30, ge=0.0, le=1.0)
    corpus_reuse_min_document_score: float = Field(default=0.22, ge=0.0, le=1.0)
    corpus_reuse_sufficient_items: int = Field(default=18, ge=1, le=200)

    # Time-to-live do corpus historico em dias. Documentos mais antigos que
    # isso (pela data de publicacao, ou pela primeira coleta quando a data
    # e desconhecida) nao sao reaproveitados: custariam LLM para revalidar
    # conteudo provavelmente desatualizado. Projetos com janela de datas
    # explicita continuam reaproveitando documentos dentro da janela pedida.
    # 0 desliga a validade.
    corpus_reuse_max_age_days: int = Field(default=180, ge=0, le=3650)

    max_fact_source_chars: int = Field(default=16000, ge=1000, le=100000)

    # LLM item budgets. Keeping these close to the raw corpus ceiling avoids a
    # search explosion becoming an OpenAI-cost explosion later in the pipeline.
    max_semantic_reviews: int = Field(default=60, ge=1, le=500)
    max_fact_extractions: int = Field(default=30, ge=1, le=200)
    max_classifications: int = Field(default=60, ge=1, le=500)
    max_cross_validations: int = Field(default=20, ge=0, le=200)

    validation_batch_size: int = Field(default=10, ge=1, le=40)
    classification_batch_size: int = Field(default=10, ge=1, le=40)
    validation_item_max_chars: int = Field(default=4000, ge=500, le=50000)
    classification_item_max_chars: int = Field(default=6000, ge=500, le=50000)

    # Video search is bounded per task. max_youtube_results_total is retained
    # for configuration compatibility, but it no longer causes raw hits to be
    # discarded after they have been returned by a provider.
    max_youtube_tasks: int = Field(default=12, ge=1, le=50)
    max_youtube_results_total: int = Field(default=120, ge=1, le=1000)
    max_youtube_results_per_task: int = Field(default=5, ge=1, le=10)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

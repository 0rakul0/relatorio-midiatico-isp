from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao"
    tavily_api_key: str | None = None
    youtube_api_key: str | None = None

    # A v2.2 usa somente OpenAI. Mantemos um único modelo configurável.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    app_env: str = "development"
    max_search_results: int = 10
    max_fact_source_chars: int = 16000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

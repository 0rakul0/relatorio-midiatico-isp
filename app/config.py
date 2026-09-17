from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao"
    tavily_api_key: str | None = None

    # A v2.2 usa somente OpenAI. Mantemos um único modelo configurável.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    # YouTube Data API e o terceiro nivel da cadeia de video:
    # DuckDuckGo -> Tavily -> YouTube Data API.
    youtube_api_key: str | None = None

    # Compatibilidade: limite bruto que o provedor pode devolver por tarefa.
    youtube_web_search_max_results: int = 15

    app_env: str = "development"
    # Limite GLOBAL de novos itens coletados pela etapa web.
    # Ex.: MAX_SEARCH_RESULTS=30 significa no máximo 30 novos itens no total,
    # e não 30 itens por consulta.
    max_search_results: int = 30

    # Evita uma única consulta consumir todo o orçamento da coleta.
    max_results_per_query: int = 5

    # Limita quantas consultas o planejador pode criar/executar na primeira coleta.
    # As consultas prioritárias por portal são preservadas antes das complementares.
    max_search_queries: int = 12

    # Quantas consultas complementares a IA pode sugerir dentro do limite acima.
    max_llm_search_queries: int = 4

    # Compatibilidade com .env anterior. O DuckDuckGo agora é o provedor
    # PRIMÁRIO; a coleta usa MAX_SEARCH_QUERIES como teto de intenções.
    max_duckduckgo_fallback_queries: int = 0
    duckduckgo_region: str = "br-pt"
    duckduckgo_safesearch: str = "moderate"
    duckduckgo_max_retries: int = 2
    duckduckgo_retry_base_seconds: float = 0.8
    duckduckgo_fetch_pages: bool = True
    duckduckgo_fetch_max_chars: int = 12000
    duckduckgo_fetch_timeout_seconds: float = 10.0

    # Orçamento total da descoberta inicial: buscas e análise estruturada do
    # perfil somadas. Com OpenAI configurada, uma chamada fica reservada para
    # a análise final e as demais para pesquisa.
    max_profile_discovery_calls: int = 4

    max_fact_source_chars: int = 16000

    # Orçamento de ITENS de IA por execução. A validação e a classificação
    # usam lotes; portanto estes limites controlam quantos itens podem ser
    # processados, e não quantas chamadas HTTP serão feitas.
    max_semantic_reviews: int = 40
    max_fact_extractions: int = 40
    max_classifications: int = 40
    max_cross_validations: int = 20

    # Tamanho dos lotes enviados à OpenAI. Ex.: 40 itens com batch_size=10
    # geram, no máximo, 4 chamadas na validação e 4 na classificação.
    validation_batch_size: int = 10
    classification_batch_size: int = 10

    # Limita o volume textual por item dentro de cada lote.
    validation_item_max_chars: int = 4000
    classification_item_max_chars: int = 6000

    # Limites globais do YouTube. Evitam que N tarefas x M resultados
    # criem centenas de itens antes da validacao.
    # Canais prioritários são sempre verificados; o padrão deixa espaço para
    # os nove canais e algumas buscas temáticas.
    max_youtube_tasks: int = 12
    max_youtube_results_total: int = 20
    max_youtube_results_per_task: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

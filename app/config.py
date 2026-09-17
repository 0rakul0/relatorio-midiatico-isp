from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao"
    tavily_api_key: str | None = None

    # A v2.2 usa somente OpenAI. Mantemos um único modelo configurável.
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    # Modelo usado pelo agente de pesquisa no YouTube via Responses API.
    # Opcional: YouTube Data API v3. Se ausente/indisponível, o agente usa OpenAI Web Search.
    youtube_api_key: str | None = None
    youtube_search_model: str = "gpt-5-mini"
    # Modelo usado pelo fallback geral de pesquisa web (sites/portais).
    web_search_model: str = "gpt-5-mini"
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

    # Orçamento de consultas OpenAI Web Search usadas como fallback.
    # 0 = herdar MAX_SEARCH_QUERIES. Quando o Tavily retorna erro duro de
    # plano/quota/autenticacao, o circuit breaker libera automaticamente
    # todas as consultas restantes da execucao para o Web Search.
    max_web_search_fallback_queries: int = 0

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
    max_youtube_tasks: int = 8
    max_youtube_results_total: int = 20
    max_youtube_results_per_task: int = 5

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

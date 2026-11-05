import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("LOCAL_AUTH_BYPASS", "false")
# Segredo ficticio apenas para testes que exercitam o lifespan da aplicacao.
# Nunca usado fora do ambiente pytest.
os.environ.setdefault("LOCAL_SESSION_SECRET", "TEST_ONLY_DO_NOT_USE_IN_PRODUCTION_1234567890abcdef")

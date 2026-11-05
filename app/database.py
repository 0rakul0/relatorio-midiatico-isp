import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


_database_url = get_settings().database_url


def local_sqlite_path() -> Path | None:
    """Retorna caminho SQLite real ou None para bancos remotos/em memoria."""
    url = make_url(_database_url)
    if not url.get_backend_name().startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    return Path(database).expanduser().resolve()


def prepare_local_database_directory() -> None:
    """Garante que o SQLite tenha pasta antes da primeira conexao."""
    path = local_sqlite_path()
    if path is None:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and path.parent.name == "data":
        path.parent.chmod(0o700)


prepare_local_database_directory()


def _engine_kwargs() -> dict:
    if _database_url.startswith("sqlite"):
        # SQLite aceita uma única escrita por vez. O timeout do driver se soma
        # ao PRAGMA busy_timeout abaixo e evita falhas imediatas sob concorrência.
        # check_same_thread=False é necessário porque o pipeline usa workers de
        # I/O e a telemetria de custo possui um gravador assíncrono próprio.
        return {
            "connect_args": {
                "timeout": 30,
                "check_same_thread": False,
            }
        }
    settings = get_settings()
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        # Pooler (Supavisor/pgbouncer) multiplexa sessoes: prepared
        # statements server-side colidem ("_pg3_0 already exists").
        # Desliga o prepare do psycopg; custo irrelevante p/ a carga.
        "connect_args": {"prepare_threshold": None},
    }


engine = create_engine(_database_url, pool_pre_ping=True, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _sqlite_on_connect(dbapi_connection, connection_record) -> None:
    """Reduz ``database is locked`` em SQLite multithread.

    WAL permite leituras durante escritas e serializa escritores concorrentes;
    busy_timeout faz cada conexão esperar a trava em vez de falhar na hora.
    """
    pragmas = [
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=30000",
        "PRAGMA cache_size=-8000",
    ]
    cursor = dbapi_connection.cursor()
    for statement in pragmas:
        try:
            cursor.execute(statement)
        except Exception:
            # A troca de journal_mode pode falhar se outra conexão segurar a
            # base no instante da conexão; as demais pragmas são idempotentes.
            continue
    cursor.close()


if _database_url.startswith("sqlite"):
    event.listen(engine, "connect", _sqlite_on_connect)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
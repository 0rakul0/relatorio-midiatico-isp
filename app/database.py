import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


_database_url = get_settings().database_url

# Somente SQLite. URLs remotas antigas falham ANTES de qualquer conexao
# e sem imprimir credenciais.
if make_url(_database_url).get_backend_name() != "sqlite":
    raise RuntimeError(
        "Banco externo nao permitido: este projeto utiliza apenas SQLite local. "
        "Em .env configure DATABASE_URL=sqlite+pysqlite:///./data/repercussao.db. "
        "Remova DATABASE_URL exportado no shell/systemd. "
        "O arquivo .env.local nao e utilizado."
    )


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
    # O pipeline paralelo faz leituras em varias threads; o SQLite serializa
    # escritas com WAL e busy_timeout.
    return {
        "connect_args": {
            "timeout": 30,
            "check_same_thread": False,
        }
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
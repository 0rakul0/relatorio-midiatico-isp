from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


_database_url = get_settings().database_url

engine = create_engine(_database_url, pool_pre_ping=True)
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
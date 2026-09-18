"""Migração aditiva para permitir testar a v2 sobre o banco atual sem perda de corpus.

Compatível com PostgreSQL e SQLite. O SQLite não permite ``ALTER TABLE ... ADD
COLUMN`` com defaults não constantes como ``CURRENT_TIMESTAMP``. Por isso,
colunas desse tipo são adicionadas sem o default no SQLite, recebem backfill e
passam a ser preenchidas pelo ORM nas novas inserções.

Para produção, mantenha backups e converta estas mudanças para sua rotina de
migrations (por exemplo, Alembic).
"""

from sqlalchemy import inspect, text

from app.database import Base, engine
import app.models  # registra todos os modelos no metadata


ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "projects": {
        "event_start": "DATE",
        "event_end": "DATE",
        "project_type": "VARCHAR(40) DEFAULT 'AUTO'",
        "topic_profile": "JSON",
        "execution_profile": "VARCHAR(40) DEFAULT 'AUTO'",
        "execution_options": "JSON",
        "fact_grace_days": "INTEGER DEFAULT 10",
        "has_custom_date_window": "BOOLEAN DEFAULT FALSE",
        "youtube_collection_status": "VARCHAR(40) DEFAULT 'NOT_ATTEMPTED'",
        "youtube_collection_error": "TEXT",
    },
    "official_facts": {
        "indicator": "VARCHAR(250)",
        "geography": "VARCHAR(200)",
        "period_start": "DATE",
        "period_end": "DATE",
        "unit": "VARCHAR(100)",
    },
    "search_queries": {
        "purpose": "VARCHAR(50) DEFAULT 'MEDIA_REPERCUSSION'",
        "execution_status": "VARCHAR(30) DEFAULT 'PENDING'",
        "execution_error": "TEXT",
        "providers_attempted": "JSON",
        "results_returned": "INTEGER DEFAULT 0",
        "results_accepted": "INTEGER DEFAULT 0",
    },
    "media_items": {
        "source_name": "VARCHAR(300)",
        "view_count": "INTEGER",
        "source_provenance": "JSON",
        "cross_validation_status": "VARCHAR(40) DEFAULT 'NOT_APPLICABLE'",
        "cross_validation_detail": "TEXT",
        "discovery_purposes": "JSON",
        "fact_status": "VARCHAR(40) DEFAULT 'PENDING'",
        "fact_discard_reason": "TEXT",
        "event_date_hint": "DATE",
        "retrieved_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "generated_reports": {
        "qa_status": "VARCHAR(30) DEFAULT 'PENDING'",
        "qa_findings": "JSON",
    },
    "fact_events": {
        "death_place_name": "VARCHAR(300)",
        "death_address": "TEXT",
        "death_neighborhood": "VARCHAR(200)",
        "death_city": "VARCHAR(200)",
        "death_state": "VARCHAR(50)",
    },
}


def _ddl_for_dialect(dialect_name: str, column_name: str, ddl: str) -> str:
    """Adapta DDLs que o SQLite não aceita em ``ADD COLUMN``.

    SQLite permite DEFAULT constante em ADD COLUMN, mas rejeita expressões como
    CURRENT_TIMESTAMP. Para ``retrieved_at`` adicionamos apenas TIMESTAMP e
    fazemos o backfill logo depois. Novas linhas recebem o valor pelo default
    Python definido no modelo ORM.
    """
    if dialect_name == "sqlite" and column_name == "retrieved_at":
        return "TIMESTAMP"
    if dialect_name == "sqlite" and column_name == "has_custom_date_window":
        return "BOOLEAN DEFAULT 0"
    return ddl


def ensure_schema() -> None:
    # Cria tabelas novas (fact_events/fact_assertions) sem tocar nas existentes.
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    dialect_name = engine.dialect.name

    with engine.begin() as connection:
        for table_name, columns in ADDITIVE_COLUMNS.items():
            if table_name not in existing_tables:
                continue

            present = {
                column["name"]
                for column in inspect(connection).get_columns(table_name)
            }

            for column_name, ddl in columns.items():
                if column_name in present:
                    continue

                effective_ddl = _ddl_for_dialect(
                    dialect_name,
                    column_name,
                    ddl,
                )

                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {effective_ddl}"
                    )
                )
                present.add(column_name)

        # Backfill seguro para projetos antigos.
        if "projects" in existing_tables:
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET event_start = collection_start "
                    "WHERE event_start IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET event_end = collection_end "
                    "WHERE event_end IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET project_type = 'AUTO' "
                    "WHERE project_type IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET execution_profile = 'AUTO' "
                    "WHERE execution_profile IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET execution_options = '{}' "
                    "WHERE execution_options IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET fact_grace_days = 10 "
                    "WHERE fact_grace_days IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE projects "
                    "SET youtube_collection_status = 'NOT_ATTEMPTED' "
                    "WHERE youtube_collection_status IS NULL"
                )
            )

        if "search_queries" in existing_tables:
            connection.execute(
                text(
                    "UPDATE search_queries "
                    "SET purpose = 'MEDIA_REPERCUSSION' "
                    "WHERE purpose IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE search_queries "
                    "SET execution_status = CASE "
                    "WHEN executed_at IS NULL THEN 'PENDING' "
                    "ELSE 'SUCCEEDED' END "
                    "WHERE execution_status IS NULL OR execution_status = 'PENDING'"
                )
            )

        if "media_items" in existing_tables:
            connection.execute(
                text(
                    "UPDATE media_items "
                    "SET fact_status = 'PENDING' "
                    "WHERE fact_status IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE media_items "
                    "SET cross_validation_status = 'NOT_APPLICABLE' "
                    "WHERE cross_validation_status IS NULL"
                )
            )
            connection.execute(
                text(
                    "UPDATE media_items "
                    "SET retrieved_at = CURRENT_TIMESTAMP "
                    "WHERE retrieved_at IS NULL"
                )
            )

        if "generated_reports" in existing_tables:
            connection.execute(
                text(
                    "UPDATE generated_reports "
                    "SET qa_status = 'PENDING' "
                    "WHERE qa_status IS NULL"
                )
            )

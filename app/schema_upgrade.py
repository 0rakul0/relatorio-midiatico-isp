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
        "owner_id": "VARCHAR(60)",
        "project_type": "VARCHAR(40) DEFAULT 'AUTO'",
        "topic_profile": "JSON",
        "execution_profile": "VARCHAR(40) DEFAULT 'AUTO'",
        "execution_options": "JSON",
        "execution_plan": "JSON",
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
        "corpus_document_id": "INTEGER",
        "corpus_origin": "VARCHAR(30) DEFAULT 'SEARCH'",
        "media_origin": "VARCHAR(30) DEFAULT 'PORTAL_NOTICIAS'",
        "relation_type": "VARCHAR(50)",
        "relevance_evidence": "TEXT",
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
        "current_version_id": "INTEGER",
        "version_no": "INTEGER DEFAULT 1",
        "content_hash": "VARCHAR(64)",
        "content_fingerprint": "VARCHAR(64)",
        "request_fingerprint": "VARCHAR(64)",
    },
    "search_hits": {
        "media_origin": "VARCHAR(30) DEFAULT 'PORTAL_NOTICIAS'",
    },
    "corpus_documents": {
        "media_origin": "VARCHAR(30) DEFAULT 'PORTAL_NOTICIAS'",
        "content_hash": "VARCHAR(64)",
        "content_fingerprint": "VARCHAR(64)",
        "alternate_urls": "JSON",
        "embedding": "JSON",
        "embedding_model": "VARCHAR(120)",
        "embedded_at": "TIMESTAMP",
    },
    "project_corpus_links": {
        "semantic_score": "FLOAT",
        "reranker_score": "FLOAT",
    },
    "fact_assertions": {
        "reported_at": "DATE",
    },
    "academic_papers": {
        "title_ptbr": "TEXT",
        "abstract_ptbr": "TEXT",
        "original_language": "VARCHAR(20)",
    },
    "fact_events": {
        "operation_name": "VARCHAR(300)",
        "death_place_name": "VARCHAR(300)",
        "death_address": "TEXT",
        "death_neighborhood": "VARCHAR(200)",
        "death_city": "VARCHAR(200)",
        "death_state": "VARCHAR(50)",
    },
}


def _ddl_for_dialect(dialect_name: str, column_name: str, ddl: str) -> str:
    if dialect_name == "sqlite" and column_name == "retrieved_at":
        return "TIMESTAMP"
    if dialect_name == "sqlite" and column_name == "has_custom_date_window":
        return "BOOLEAN DEFAULT 0"
    return ddl


def ensure_schema() -> None:
    dialect_name = engine.dialect.name

    # Todas as alterações de schema ficam no mesmo transaction scope. Em
    # PostgreSQL, o advisory lock serializa startups concorrentes apontando
    # para o mesmo banco (por exemplo, dois processos uvicorn/reload ou duas
    # estações usando o mesmo Supabase).
    with engine.begin() as connection:
        if dialect_name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(741983214)")
            )

        # Cria tabelas novas usando a MESMA conexão protegida pelo lock.
        Base.metadata.create_all(bind=connection)

        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())

        if dialect_name == "postgresql":
            # Defesa em profundidade no Supabase/bancos gerenciados: habilita
            # RLS somente onde ainda está desativado.
            unprotected = connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND rowsecurity = false"
                )
            ).scalars().all()
            for table_name in unprotected:
                if table_name in Base.metadata.tables:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
                    )

            # REVOKE altera o catálogo do PostgreSQL. Fazer isso em todo startup
            # sem verificar o estado causava 'tuple concurrently updated' no
            # Supabase. Agora só revogamos quando o grant realmente existe,
            # ainda sob o advisory lock acima.
            granted_pairs = connection.execute(
                text(
                    "SELECT grantee, table_name "
                    "FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'public' "
                    "AND grantee IN ('anon', 'authenticated')"
                )
            ).all()
            app_tables = set(Base.metadata.tables)
            for role, table_name in granted_pairs:
                if table_name in app_tables:
                    connection.execute(
                        text(f"REVOKE ALL ON TABLE {table_name} FROM {role}")
                    )

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
                effective_ddl = _ddl_for_dialect(dialect_name, column_name, ddl)
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN {column_name} {effective_ddl}"
                    )
                )
                present.add(column_name)

        if "corpus_documents" in existing_tables:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_corpus_documents_content_hash "
                    "ON corpus_documents (content_hash)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_corpus_documents_content_fingerprint "
                    "ON corpus_documents (content_fingerprint)"
                )
            )

        # Bancos PostgreSQL legados podem ter Classification.framing como
        # VARCHAR(300). O conteúdo analítico deve ser TEXT.
        if dialect_name == "postgresql" and "classifications" in existing_tables:
            framing_type = next(
                (
                    column["type"]
                    for column in inspect(connection).get_columns("classifications")
                    if column["name"] == "framing"
                ),
                None,
            )
            if framing_type is not None and str(framing_type).upper() != "TEXT":
                connection.execute(
                    text("ALTER TABLE classifications ALTER COLUMN framing TYPE TEXT")
                )

        if "projects" in existing_tables:
            connection.execute(text("UPDATE projects SET event_start = collection_start WHERE event_start IS NULL"))
            connection.execute(text("UPDATE projects SET event_end = collection_end WHERE event_end IS NULL"))
            connection.execute(text("UPDATE projects SET project_type = 'AUTO' WHERE project_type IS NULL"))
            connection.execute(text("UPDATE projects SET execution_profile = 'AUTO' WHERE execution_profile IS NULL"))
            connection.execute(text("UPDATE projects SET execution_options = '{}' WHERE execution_options IS NULL"))
            connection.execute(text("UPDATE projects SET execution_plan = '{}' WHERE execution_plan IS NULL"))
            connection.execute(text("UPDATE projects SET fact_grace_days = 10 WHERE fact_grace_days IS NULL"))
            connection.execute(text("UPDATE projects SET youtube_collection_status = 'NOT_ATTEMPTED' WHERE youtube_collection_status IS NULL"))

        if "search_queries" in existing_tables:
            connection.execute(text("UPDATE search_queries SET purpose = 'MEDIA_REPERCUSSION' WHERE purpose IS NULL"))
            connection.execute(
                text(
                    "UPDATE search_queries "
                    "SET execution_status = CASE WHEN executed_at IS NULL THEN 'PENDING' ELSE 'SUCCEEDED' END "
                    "WHERE execution_status IS NULL OR execution_status = 'PENDING'"
                )
            )

        if "media_items" in existing_tables:
            connection.execute(text("UPDATE media_items SET fact_status = 'PENDING' WHERE fact_status IS NULL"))
            connection.execute(text("UPDATE media_items SET cross_validation_status = 'NOT_APPLICABLE' WHERE cross_validation_status IS NULL"))
            connection.execute(text("UPDATE media_items SET corpus_origin = 'HISTORICAL' WHERE corpus_origin IS NULL"))
            connection.execute(text("UPDATE media_items SET retrieved_at = CURRENT_TIMESTAMP WHERE retrieved_at IS NULL"))
            connection.execute(text("UPDATE media_items SET media_origin = 'PORTAL_NOTICIAS' WHERE media_origin IS NULL"))

        if "search_hits" in existing_tables:
            connection.execute(text("UPDATE search_hits SET media_origin = 'PORTAL_NOTICIAS' WHERE media_origin IS NULL"))

        if "corpus_documents" in existing_tables:
            connection.execute(text("UPDATE corpus_documents SET media_origin = 'PORTAL_NOTICIAS' WHERE media_origin IS NULL"))

        if "generated_reports" in existing_tables:
            connection.execute(text("UPDATE generated_reports SET qa_status = 'PENDING' WHERE qa_status IS NULL"))

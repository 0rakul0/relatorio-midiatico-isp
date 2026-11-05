"""Inicializa e valida o SQLite local usado por login e corpus.

Uso (raiz do projeto): uv run python -m app.init_db
Nao inclui senhas nem cria uma conta padrao insegura.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import func, inspect, select

from app.database import SessionLocal, engine, local_sqlite_path
from app.models import AppUser
from app.schema_upgrade import ensure_schema


REQUIRED_USER_FIELDS = frozenset(
    {"id", "email", "password_hash", "is_admin", "plan", "created_at"}
)


def initialize_database() -> dict[str, object]:
    """Prepara tabelas idempotentemente sem apagar dados existentes."""
    ensure_schema()
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if "app_users" not in tables:
        raise RuntimeError("Tabela app_users nao foi criada")
    fields = {column["name"] for column in inspector.get_columns("app_users")}
    missing = REQUIRED_USER_FIELDS - fields
    if missing:
        raise RuntimeError("Schema de login incompleto: " + ", ".join(sorted(missing)))

    file_path = local_sqlite_path()
    if file_path is not None:
        if not file_path.is_file():
            raise RuntimeError(f"Arquivo SQLite nao encontrado: {file_path}")
        if os.name == "posix":
            file_path.chmod(0o600)

    with SessionLocal() as session:
        user_count = int(session.scalar(select(func.count()).select_from(AppUser)) or 0)
        admin_count = int(session.scalar(
            select(func.count()).select_from(AppUser).where(AppUser.is_admin.is_(True))
        ) or 0)

    return {
        "database": str(file_path) if file_path else engine.url.get_backend_name(),
        "tables": len(tables),
        "users": user_count,
        "admins": admin_count,
    }


def main() -> None:
    if os.name == 'posix':
        os.umask(0o077)
    state = initialize_database()
    print(f"Banco inicializado: {state['database']}")
    print(f"Tabelas: {state['tables']} | Usuarios: {state['users']} | Administradores: {state['admins']}")
    if not state["admins"]:
        print("Crie o primeiro administrador: uv run python -m app.local_users create --email seu-email@exemplo.br --admin")


if __name__ == "__main__":
    main()

"""Diagnostico local seguro do login (sem exibir senha, tokens ou chaves).

Uso:
    uv run python -m app.check_auth --email usuario@exemplo.com
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from app.config import get_settings
from app.database import local_sqlite_path


def inspect_login(email: str | None = None) -> int:
    settings = get_settings()
    path = local_sqlite_path()
    secret_ok = len((settings.local_session_secret or "").strip().encode("utf-8")) >= 32
    print(f"Ambiente: {settings.app_env}")
    print(f"Bypass local: {'ATIVO' if settings.local_auth_bypass else 'desativado'}")
    print(f"LOCAL_SESSION_SECRET: {'configurada' if secret_ok else 'AUSENTE/INVALIDA'}")
    print(f"Banco SQLite: {path if path is not None else 'nao e arquivo local'}")

    if path is None or not path.is_file():
        print("Banco ainda nao criado. Execute: uv run python -m app.init_db")
        return 1
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_users'"
            ).fetchone()
            if not found:
                print("Tabela app_users nao encontrada. Execute: uv run python -m app.init_db")
                return 1
            count = conn.execute("SELECT count(*) FROM app_users").fetchone()[0]
            print(f"Contas locais: {count}")
            if email:
                row = conn.execute(
                    "SELECT (password_hash IS NOT NULL AND length(password_hash) > 0), is_admin "
                    "FROM app_users WHERE lower(email)=lower(?)",
                    (email.strip(),),
                ).fetchone()
                if row:
                    print("Usuario solicitado: encontrado")
                    print(f"Hash de senha: {'configurado' if row[0] else 'AUSENTE'}")
                    print(f"Administrador: {'sim' if row[1] else 'nao'}")
                else:
                    print("Usuario solicitado: NAO ENCONTRADO neste banco")
                    return 1
    except sqlite3.Error:
        print("Nao foi possivel consultar a tabela de usuarios (verifique o banco/permissoes).")
        return 1

    if settings.local_auth_bypass or not secret_ok:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Verifica configuracao e usuario local sem mostrar segredos")
    parser.add_argument("--email", help="Conta que deve existir no banco (opcional)")
    args = parser.parse_args()
    raise SystemExit(inspect_login(args.email))


if __name__ == "__main__":
    main()

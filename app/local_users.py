"""Criacao e atualizacao de usuarios locais sem cadastro publico.

Exemplo:
    uv run python -m app.local_users create --email admin@example.org --admin
"""
from __future__ import annotations

import argparse
import getpass
import re
from uuid import uuid4

from sqlalchemy import func, select

from app.auth import hash_password
from app.database import SessionLocal
from app.models import AppUser
from app.schema_upgrade import ensure_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Gerenciamento de usuarios locais")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Cria conta ou redefine senha da conta existente")
    create.add_argument("--email", required=True)
    create.add_argument("--admin", action="store_true", help="Concede perfil administrativo")
    args = parser.parse_args()

    if args.command != "create":
        parser.error("Comando invalido")
    email = args.email.strip().casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
        parser.error("Informe um email valido")

    password = getpass.getpass("Nova senha (minimo 12 caracteres): ")
    repeat = getpass.getpass("Repita a senha: ")
    if password != repeat:
        parser.error("As senhas nao coincidem")
    try:
        encoded = hash_password(password)
    except ValueError as exc:
        parser.error(str(exc))

    ensure_schema()
    with SessionLocal() as db:
        row = db.scalar(select(AppUser).where(func.lower(AppUser.email) == email))
        if row is None:
            row = AppUser(
                id=uuid4().hex,
                email=email,
                plan="INSTITUCIONAL" if args.admin else "FREE",
                is_admin=bool(args.admin),
            )
            db.add(row)
        elif args.admin:
            row.is_admin = True
            row.plan = "INSTITUCIONAL"
        row.password_hash = encoded
        db.commit()
        print(f"Usuario configurado: {row.email} (admin={bool(row.is_admin)})")


if __name__ == "__main__":
    main()

"""Contrato do login local sem dependencias de Supabase ou rede."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_current_user, hash_password, router, verify_password
from app.database import Base, get_db
from app.models import AppUser


def _client(monkeypatch):
    monkeypatch.setattr(
        "app.auth.get_settings",
        lambda: SimpleNamespace(
            local_auth_bypass=False,
            local_session_secret="test-only-secret-not-for-production-" * 2,
            app_env="production",
        ),
    )
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    api = FastAPI()
    api.include_router(router)

    def override_db():
        with factory() as db:
            yield db

    api.dependency_overrides[get_db] = override_db
    return TestClient(api), factory, engine


def test_password_hash_is_salted():
    first = hash_password("example-password-123")
    second = hash_password("example-password-123")
    assert first != second
    assert verify_password("example-password-123", first)
    assert not verify_password("incorrect-password", first)
    assert not verify_password("anything", "malformed")


def test_local_login_requires_password_and_expires_after_change(monkeypatch):
    client, factory, engine = _client(monkeypatch)
    try:
        with factory() as db:
            db.add(
                AppUser(
                    id="u1",
                    email="admin@example.org",
                    is_admin=True,
                    plan="INSTITUCIONAL",
                    password_hash=hash_password("correct-password-123"),
                )
            )
            db.commit()

        with client:
            assert client.get("/auth/me").status_code == 401
            assert client.post(
                "/auth/login",
                json={"email": "admin@example.org", "password": "wrong"},
            ).status_code == 401
            login = client.post(
                "/auth/login",
                json={"email": "ADMIN@EXAMPLE.ORG", "password": "correct-password-123"},
            )
            assert login.status_code == 200
            token = login.json()["access_token"]
            assert login.json()["expires_in"] == 43200
            headers = {"Authorization": f"Bearer {token}"}
            me = client.get("/auth/me", headers=headers)
            assert me.status_code == 200
            assert me.json()["is_admin"] is True
            altered = "v1.A" + token.split(".", 2)[1][1:] + "." + token.rsplit(".", 1)[1]
            assert client.get(
                "/auth/me", headers={"Authorization": f"Bearer {altered}"}
            ).status_code == 401

            with factory() as db:
                db.get(AppUser, "u1").password_hash = hash_password("changed-password-123")
                db.commit()
            assert client.get("/auth/me", headers=headers).status_code == 401
    finally:
        engine.dispose()


def test_no_public_signup(monkeypatch):
    client, _, engine = _client(monkeypatch)
    try:
        with client:
            assert client.post("/auth/signup", json={}).status_code == 404
    finally:
        engine.dispose()

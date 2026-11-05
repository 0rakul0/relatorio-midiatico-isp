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
            local_signup_enabled=True,
            local_signup_max_per_hour=5,
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
    monkeypatch.setattr("app.auth._signup_attempts", {})

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


def test_signup_creates_free_user_and_preserves_admin(monkeypatch):
    from app.auth import verify_password

    client, factory, engine = _client(monkeypatch)
    try:
        with factory() as db:
            db.add(
                AppUser(
                    id="owner", email="admin@example.org", is_admin=True,
                    plan="INSTITUCIONAL", password_hash=hash_password("admin-password-123"),
                )
            )
            db.commit()

        with client:
            created = client.post(
                "/auth/signup",
                json={"email": " New.User@Example.org ", "password": "a-long-password-123"},
            )
            assert created.status_code == 201, created.text
            payload = created.json()
            assert payload["email"] == "new.user@example.org"
            assert payload["plan"] == "FREE"
            assert "access_token" in payload
            current = client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            assert current.status_code == 200
            assert current.json()["is_admin"] is False
            assert current.json()["plan"] == "FREE"
            assert current.json()["email"] == "new.user@example.org"

            regular_login = client.post(
                "/auth/login",
                json={"email": "new.user@example.org", "password": "a-long-password-123"},
            )
            assert regular_login.status_code == 200

            with factory() as db:
                admin = db.get(AppUser, "owner")
                assert admin.is_admin is True
                assert admin.plan == "INSTITUCIONAL"
                registered = db.scalar(
                    __import__("sqlalchemy").select(AppUser).where(
                        AppUser.email == "new.user@example.org"
                    )
                )
                assert registered is not None
                assert registered.id != admin.id
                assert verify_password("a-long-password-123", registered.password_hash)
                assert registered.password_hash != "a-long-password-123"

            duplicate = client.post(
                "/auth/signup",
                json={"email": "NEW.USER@example.org", "password": "another-password-123"},
            )
            assert duplicate.status_code == 409
            duplicate_admin = client.post(
                "/auth/signup",
                json={"email": "ADMIN@example.org", "password": "another-password-123"},
            )
            assert duplicate_admin.status_code == 409
            assert "access_token" not in duplicate_admin.json()
    finally:
        engine.dispose()


def test_signup_rejects_role_injection_and_weak_password(monkeypatch):
    client, factory, engine = _client(monkeypatch)
    try:
        with client:
            weak = client.post(
                "/auth/signup", json={"email": "test@example.org", "password": "short"}
            )
            assert weak.status_code == 422
            elevated = client.post(
                "/auth/signup",
                json={
                    "email": "test@example.org", "password": "long-password-123",
                    "is_admin": True, "plan": "INSTITUCIONAL",
                },
            )
            assert elevated.status_code == 422
            with factory() as db:
                assert db.query(AppUser).count() == 0
    finally:
        engine.dispose()


def test_signup_can_be_disabled(monkeypatch):
    from types import SimpleNamespace

    client, _, engine = _client(monkeypatch)
    monkeypatch.setattr(
        "app.auth.get_settings",
        lambda: SimpleNamespace(
            local_auth_bypass=False,
            local_signup_enabled=False,
            local_signup_max_per_hour=5,
            local_session_secret="test-only-secret-not-for-production-" * 2,
            app_env="production",
        ),
    )
    try:
        with client:
            forbidden = client.post(
                "/auth/signup",
                json={"email": "new@example.org", "password": "a-long-password-123"},
            )
            assert forbidden.status_code == 403
            assert client.post(
                "/auth/login",
                json={"email": "new@example.org", "password": "a-long-password-123"},
            ).status_code == 401
    finally:
        engine.dispose()


def test_signup_rate_limit(monkeypatch):
    from types import SimpleNamespace

    client, factory, engine = _client(monkeypatch)
    monkeypatch.setattr(
        "app.auth.get_settings",
        lambda: SimpleNamespace(
            local_auth_bypass=False,
            local_signup_enabled=True,
            local_signup_max_per_hour=2,
            local_session_secret="test-only-secret-not-for-production-" * 2,
            app_env="production",
        ),
    )
    try:
        with client:
            for n in range(2):
                ok = client.post(
                    "/auth/signup",
                    json={"email": f"user{n}@example.org", "password": "long-password-123"},
                )
                assert ok.status_code == 201, ok.text
            limited = client.post(
                "/auth/signup",
                json={"email": "third@example.org", "password": "long-password-123"},
            )
            assert limited.status_code == 429
            with factory() as db:
                assert db.query(AppUser).count() == 2
    finally:
        engine.dispose()


def test_signup_recovers_from_temporary_sqlite_lock(monkeypatch):
    """A primeira escrita falha como se outra demanda estivesse gravando."""
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    client, factory, engine = _client(monkeypatch)
    original_commit = Session.commit
    attempts = {"count": 0}

    def busy_once(self):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OperationalError(
                "INSERT INTO app_users", {},
                sqlite3.OperationalError("database is locked"),
            )
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", busy_once)
    monkeypatch.setattr("app.auth._SIGNUP_RETRY_DELAYS", (0, 0))
    try:
        with client:
            response = client.post(
                "/auth/signup",
                json={"email": "busy@example.org", "password": "a-long-password-123"},
            )
            assert response.status_code == 201, response.text
            assert attempts["count"] == 2
            with factory() as db:
                user = db.scalar(
                    __import__("sqlalchemy").select(AppUser).where(
                        AppUser.email == "busy@example.org"
                    )
                )
                assert user is not None
                assert user.is_admin is False
                assert user.plan == "FREE"
    finally:
        engine.dispose()


def test_signup_reports_busy_as_retryable_503_not_500(monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    client, factory, engine = _client(monkeypatch)
    attempts = {"count": 0}

    def always_busy(self):
        attempts["count"] += 1
        raise OperationalError(
            "INSERT INTO app_users", {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(Session, "commit", always_busy)
    monkeypatch.setattr("app.auth._SIGNUP_RETRY_DELAYS", (0, 0))
    try:
        with client:
            response = client.post(
                "/auth/signup",
                json={"email": "busy@example.org", "password": "a-long-password-123"},
            )
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "3"
            assert "banco esta ocupado" in response.json()["detail"]
            assert attempts["count"] == 3
            with factory() as db:
                assert db.query(AppUser).count() == 0
    finally:
        engine.dispose()


def test_signup_retries_sqlite_lock_on_initial_read(monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    client, factory, engine = _client(monkeypatch)
    old_scalar = Session.scalar
    attempts = {"count": 0}

    def first_read_busy(self, *args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OperationalError(
                "SELECT app_users", {},
                sqlite3.OperationalError("database is locked"),
            )
        return old_scalar(self, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", first_read_busy)
    monkeypatch.setattr("app.auth._SIGNUP_RETRY_DELAYS", (0, 0))
    try:
        with client:
            response = client.post(
                "/auth/signup",
                json={"email": "readbusy@example.org", "password": "a-long-password-123"},
            )
            assert response.status_code == 201, response.text
            assert attempts["count"] >= 2
    finally:
        engine.dispose()

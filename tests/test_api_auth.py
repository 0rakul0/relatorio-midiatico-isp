from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.auth import AuthUser
from app.database import Base
from app.main import app
from app.models import AppUser, LLMCall, Project, ReportRun

USER_A = AuthUser(id="user-a", email="a@x.com", is_admin=False, plan="FREE")
USER_B = AuthUser(id="user-b", email="b@x.com", is_admin=False, plan="FREE")
ADMIN = AuthUser(id="admin", email="admin@x.com", is_admin=True, plan="INSTITUCIONAL")


def _client(monkeypatch, user):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(main_module, "ensure_schema", lambda: None)
    app.dependency_overrides.clear()
    if user is not None:
        app.dependency_overrides[main_module.get_current_user] = lambda: user

    def _override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    from app.database import get_db

    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app, raise_server_exceptions=False)
    client.__test_engine = engine
    return client


def _session_of(client):
    return sessionmaker(bind=client.__test_engine)()


def _make_project(client, topic="tema"):
    response = client.post("/projects", json={"topic": topic})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_unauthenticated_is_rejected(monkeypatch):
    client = _client(monkeypatch, None)
    with client:
        assert client.post("/projects", json={"topic": "x"}).status_code == 401
        assert client.get("/reports/history").status_code == 401
        assert client.get("/costs").status_code == 401


def test_ownership_isolation_and_history_scope(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        pid = _make_project(client, "tema de A")
        assert client.get(f"/projects/{pid}/metrics").status_code == 200
        assert client.get("/reports/history").json() == []

    client_b = _client(monkeypatch, USER_B)
    with client_b:
        assert client_b.get(f"/projects/{pid}/metrics").status_code == 404
        assert client_b.get("/reports/history").json() == []


def test_admin_and_owner_sharing_same_db(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        pid = _make_project(client, "tema compartilhado")
        session = _session_of(client)
        session.add(AppUser(id="admin", email="admin@x.com", plan="INSTITUCIONAL", is_admin=True))
        session.commit()
        session.close()
        # Troca o usuário mantendo o banco: admin enxerga o projeto de A.
        app.dependency_overrides[main_module.get_current_user] = lambda: ADMIN
        assert client.get(f"/projects/{pid}/metrics").status_code == 200


def test_quota_blocks_run_async(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        pid = _make_project(client, "tema com quota")
        session = _session_of(client)
        project = session.get(Project, pid)
        project.owner_id = "user-a"
        now = datetime.now(timezone.utc)
        session.add_all([
            ReportRun(run_id="r1", project_id=pid, status="COMPLETED", created_at=now),
            ReportRun(run_id="r2", project_id=pid, status="COMPLETED", created_at=now),
        ])
        session.commit()
        session.close()
        response = client.post(f"/projects/{pid}/run-async")
        assert response.status_code == 402, response.text


def test_margin_applies_to_usage_and_quota(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        pid = _make_project(client, "tema margem")
        session = _session_of(client)
        session.get(Project, pid).owner_id = "user-a"
        session.add(LLMCall(project_id=pid, model="m", success=True, cost_usd=1.0))
        session.commit()
        session.close()
        me = client.get("/billing/me").json()
        assert me["usage"]["llm_usd_raw"] == 1.0
        assert me["usage"]["llm_usd"] == 1.1
        assert me["usage"]["margin_pct"] == 10.0


def test_quota_uses_billed_cost(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        pid = _make_project(client, "tema limite")
        session = _session_of(client)
        session.get(Project, pid).owner_id = "user-a"
        # Raw abaixo do teto FREE (1.0), faturado acima (1.045): bloqueia.
        session.add(LLMCall(project_id=pid, model="m", success=True, cost_usd=0.95))
        session.commit()
        session.close()
        assert client.post(f"/projects/{pid}/run-async").status_code == 402


def test_free_plan_clamps_profile_and_features(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        response = client.post("/projects", json={
            "topic": "tema clamp",
            "execution_profile": "COMPLETO_NOMINAL",
            "enable_fact_layer": True,
            "enable_nominal_followup": True,
        })
        assert response.status_code == 201
        body = response.json()
        assert body["plan_notice"] is not None
        session = _session_of(client)
        project = session.get(Project, body["id"])
        assert project.execution_profile == "MIDIATICO_SIMPLES"
        assert project.execution_options.get("enable_fact_layer") is False
        assert project.execution_options.get("enable_nominal_followup") is False
        session.close()


def test_run_async_allowed_when_quota_ok(monkeypatch):
    client = _client(monkeypatch, USER_A)
    monkeypatch.setattr(main_module, "start_run", lambda project_id: {"run_id": "x"})
    with client:
        pid = _make_project(client, "tema ok")
        session = _session_of(client)
        session.get(Project, pid).owner_id = "user-a"
        session.commit()
        session.close()
        response = client.post(f"/projects/{pid}/run-async")
        assert response.status_code == 202, response.text


def test_billing_mock_and_admin_costs(monkeypatch):
    client = _client(monkeypatch, USER_A)
    with client:
        assert client.get("/costs").status_code == 403
        assert client.get("/billing/subscriptions").status_code == 403
        checkout = client.post("/billing/checkout", json={"plan": "PRO"})
        assert checkout.status_code == 501
        assert checkout.json()["mock"] is True
        # Usuário ainda FREE: quota de 2 vale.
        me = client.get("/billing/me").json()
        assert me["plan"] == "FREE"

        # Webhook mock promove.
        session = _session_of(client)
        session.add(AppUser(id="user-a", email="a@x.com", plan="FREE"))
        session.commit()
        session.close()
        hook = client.post("/billing/webhook", json={
            "user_id": "user-a", "plan": "PRO", "status": "active"})
        assert hook.status_code == 200
        # Próximo login lê o plano novo do banco.
        pro_user = AuthUser(id="user-a", email="a@x.com", is_admin=False, plan="PRO")
        app.dependency_overrides[main_module.get_current_user] = lambda: pro_user
        assert client.get("/billing/me").json()["plan"] == "PRO"

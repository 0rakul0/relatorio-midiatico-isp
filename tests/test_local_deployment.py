"""Inicializacao real do SQLite em um diretorio temporario (sem OpenAI/rede)."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from app import llm as llm_module


def test_init_db_creates_local_auth_tables_and_preserves_existing_file(tmp_path):
    db = tmp_path / "data" / "repercussao.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite+pysqlite:///" + db.as_posix()
    env["LOCAL_AUTH_BYPASS"] = "false"
    cmd = [sys.executable, "-m", "app.init_db"]
    cwd = Path(__file__).resolve().parents[1]

    first = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    assert db.is_file()
    assert "Usuarios: 0" in first.stdout
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(app_users)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"id", "email", "password_hash", "is_admin", "plan", "created_at"} <= columns
        assert {"app_users", "projects", "chat_conversations", "media_items"} <= tables
        conn.execute(
            "INSERT INTO app_users (id, email, plan, is_admin, password_hash) VALUES (?, ?, ?, ?, ?)",
            ("existing", "tester@example.org", "FREE", 0, "hashed"),
        )
        conn.commit()

    again = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Usuarios: 1" in again.stdout
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM app_users").fetchone()[0] == 1

    if os.name == "posix":
        assert db.stat().st_mode & 0o777 == 0o600
        assert db.parent.stat().st_mode & 0o777 == 0o700


def test_openai_only_disables_ollama_even_if_old_fallback_vars_exist(monkeypatch):
    from types import SimpleNamespace

    settings = SimpleNamespace(
        openai_only=True,
        openai_api_key="sk-test-not-real",
        openai_model="gpt-4.1-mini",
        openai_base_url=None,
        openai_reasoning_effort="low",
        openai_fallback_base_url="http://localhost:11434/v1",
        openai_fallback_model="gemma4:12b",
        openai_fallback_api_key="ollama",
    )
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    assert llm_module.llm_is_configured()
    assert llm_module._build_fallback_model(max_output_tokens=200) is None
    model = llm_module.create_chat_model(max_output_tokens=200)
    assert model._fallback_model is None

    settings.openai_api_key = None
    assert not llm_module.llm_is_configured()
    import pytest
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm_module.create_chat_model(max_output_tokens=200)


def test_remote_database_url_is_refused_without_attempting_connection():
    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql+psycopg://127.0.0.1:6543/repercussao"
    result = subprocess.run(
        [sys.executable, "-c", "import app.database"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "SQLite local" in (result.stderr + result.stdout)
    assert "ConnectionTimeout" not in (result.stderr + result.stdout)


def test_settings_load_only_dotenv():
    from app.config import Settings
    assert Settings.model_config.get("env_file") == ".env"

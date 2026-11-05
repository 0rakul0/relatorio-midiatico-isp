"""Contrato da tela de autocadastro e publicacao segura do recurso."""
from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

import pytest

from app import main as main_module


_ROOT = Path(__file__).resolve().parents[1]


def test_signup_form_is_present_in_local_login_page():
    html = (_ROOT / "app/static/login.html").read_text(encoding="utf-8")
    js = (_ROOT / "app/static/login.js").read_text(encoding="utf-8")
    assert 'id="signup-form"' in html
    assert 'id="signup-email"' in html
    assert 'id="signup-pass"' in html
    assert 'id="signup-confirm"' in html
    assert 'autocomplete="new-password"' in html
    assert 'id="mode-signup"' in html
    assert "/auth/signup" in js
    assert "signup_enabled" in js
    assert "password !== confirmed" in js
    assert "saveSession(result, email)" in js
    assert "supabase" not in js.casefold()


def test_signup_config_is_public_boolean_only(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(local_auth_bypass=False, local_signup_enabled=True),
    )
    assert main_module.auth_config() == {
        "provider": "local", "local_auth_bypass": False, "signup_enabled": True,
    }
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(local_auth_bypass=True, local_signup_enabled=True),
    )
    assert main_module.auth_config()["signup_enabled"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js indisponivel")
def test_login_javascript_has_valid_syntax():
    completed = subprocess.run(
        ["node", "--check", str(_ROOT / "app/static/login.js")],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr

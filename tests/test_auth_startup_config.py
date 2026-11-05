"""Configuracao do login local antes de levantar o servidor."""
from types import SimpleNamespace

import pytest

from app.main import validate_startup_configuration


def _settings(**overrides):
    values = dict(
        app_env="production",
        local_auth_bypass=False,
        local_session_secret="a" * 64,
        openai_only=True,
        openai_api_key="key-from-env",
        openai_base_url=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_production_requires_session_secret():
    with pytest.raises(RuntimeError, match="LOCAL_SESSION_SECRET"):
        validate_startup_configuration(_settings(local_session_secret=""))
    with pytest.raises(RuntimeError, match="LOCAL_SESSION_SECRET"):
        validate_startup_configuration(_settings(local_session_secret="short"))


def test_development_without_bypass_requires_secret():
    with pytest.raises(RuntimeError, match="LOCAL_SESSION_SECRET"):
        validate_startup_configuration(
            _settings(app_env="development", local_session_secret=None)
        )


def test_valid_secret_allows_login():
    validate_startup_configuration(_settings())


def test_production_rejects_bypass():
    with pytest.raises(RuntimeError, match="LOCAL_AUTH_BYPASS"):
        validate_startup_configuration(_settings(local_auth_bypass=True))

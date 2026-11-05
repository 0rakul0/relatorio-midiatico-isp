"""Valida se o .env.example usa nomes reais do Settings atual."""
from pathlib import Path

from app.config import Settings


def test_env_example_keys_are_declared():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    keys = [
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    ]
    known = {name.upper() for name in Settings.model_fields}
    assert len(keys) == len(set(keys)), "Variaveis duplicadas no exemplo"
    assert not (set(keys) - known), f"Chaves ignoradas: {sorted(set(keys) - known)}"
    assert {
        "OPENAI_ONLY",
        "OPENAI_API_KEY",
        "MAX_MEDIA_QUERIES",
        "MAX_OFFICIAL_QUERIES",
        "ENABLE_OFFICIAL_ANNUAL_INVENTORY",
        "SOCIAL_DISCOVERY_QUERIES_PER_PLATFORM",
        "APIFY_SOCIAL_ENABLED",
    }.issubset(keys)


def test_linux_env_example_defaults_do_not_require_ollama_or_apify():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = {}
    for raw in example.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        name, value = raw.split("=", 1)
        settings[name.strip()] = value.strip()
    assert settings["OPENAI_ONLY"] == "true"
    assert "OPENAI_FALLBACK_BASE_URL" not in settings
    assert settings["APIFY_SOCIAL_ENABLED"] == "false"
    assert settings["SOCIAL_DISCOVERY_ENABLED"] == "true"
    assert int(settings["MAX_OFFICIAL_QUERIES"]) >= 12
    assert int(settings["MAX_OFFICIAL_INVENTORY_QUERIES"]) >= 12

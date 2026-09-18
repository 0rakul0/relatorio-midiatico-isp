import pytest

from app.tools import search as tools_search
from app.tools.providers.duckduckgo import _url_is_public


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost.localdomain/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8000/admin",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://8.8.8.8@127.0.0.1/",
        "ftp://8.8.8.8/file",
    ],
)
def test_ssrf_blocked_targets(url):
    assert _url_is_public(url) is False


def test_ssrf_allows_public_ip():
    assert _url_is_public("http://8.8.8.8/") is True
    assert _url_is_public("https://1.1.1.1/path") is True


def test_tavily_circuit_breaker_opens_on_hard_failure(monkeypatch):
    tools_search.reset_tavily_circuit_breaker()
    calls = {"count": 0}

    def fake_tavily(*_args, **_kwargs):
        calls["count"] += 1
        raise RuntimeError("usage limit exceeded")

    monkeypatch.setattr(tools_search, "tavily_search", fake_tavily)
    try:
        provider, rows = tools_search._search_web("consulta", providers=("tavily",))
        assert (provider, rows) == ("none", [])
        assert tools_search.tavily_is_disabled() is True
        assert calls["count"] == 1

        # Depois de aberto, o breaker impede novas tentativas.
        tools_search._search_web("outra consulta", providers=("tavily",))
        assert calls["count"] == 1
        snapshot = tools_search.tavily_circuit_breaker_snapshot()
        assert snapshot["trips"] == 1
        assert snapshot["hard_failures"] == 1
    finally:
        tools_search.reset_tavily_circuit_breaker()

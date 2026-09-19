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


def test_duckduckgo_only_provider_without_fallback(monkeypatch):
    monkeypatch.setattr(tools_search, "duckduckgo_news", lambda *_a, **_k: [])
    monkeypatch.setattr(tools_search, "duckduckgo_text", lambda *_a, **_k: [])
    monkeypatch.setattr(tools_search, "duckduckgo_videos", lambda *_a, **_k: [])

    provider, rows = tools_search._search_web("consulta")
    assert (provider, rows) == ("duckduckgo", [])

    provider, rows = tools_search._search_videos("consulta")
    assert (provider, rows) == ("duckduckgo", [])

    assert tools_search.search_providers_available() == tools_search.duckduckgo_available()

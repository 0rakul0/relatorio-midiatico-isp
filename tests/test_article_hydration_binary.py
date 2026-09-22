import gzip

from app.tools.providers import duckduckgo as provider


def test_gzip_magic_is_valid_python_fixture():
    payload = gzip.compress(b"<html><body>texto valido</body></html>")
    assert payload[:2] == b"\x1f\x8b"


def test_nul_can_be_removed_before_postgres_text():
    value = "abc\x00def"
    assert value.replace("\x00", "") == "abcdef"

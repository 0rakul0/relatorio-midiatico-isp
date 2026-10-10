from unittest.mock import patch

from app.tools.providers.academic_multi import search_scielo


def test_scielo_direct_search_extracts_article_links_and_deduplicates():
    html = """
    <html><body>
    <a href="https://www.scielo.br/j/csc/a/rM9rtHsDqbsndhcjMnxymJS/abstract/?lang=pt">
      Analise de padroes da violencia contra mulheres no Brasil
    </a>
    <a href="https://www.scielo.br/j/csc/a/rM9rtHsDqbsndhcjMnxymJS/abstract/?lang=en">
      Analise de padroes da violencia contra mulheres no Brasil
    </a>
    <a href="https://outrosite.org/noticia">Outro site</a>
    </body></html>
    """
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, *_):
            return html.encode("utf-8")
    with patch("app.tools.providers.academic_multi.urlopen", return_value=Response()):
        rows = search_scielo("violencia contra mulheres brasil")
    assert len(rows) == 1
    assert rows[0]["provider"] == "scielo"
    assert rows[0]["is_preprint"] is False
    assert "/j/csc/a/rM9rtHsDqbsndhcjMnxymJS/" in rows[0]["url"]


def test_scielo_unavailable_raises_provider_exception():
    from app.tools.providers.academic_multi import AcademicProviderUnavailable
    with patch("app.tools.providers.academic_multi.urlopen", side_effect=OSError("indisponivel")):
        try:
            search_scielo("violencia contra mulheres")
        except AcademicProviderUnavailable:
            pass
        else:
            raise AssertionError("Deveria reportar indisponibilidade")

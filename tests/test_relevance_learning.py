from app.services.relevance_learning import (
    _hash_embedding,
    cosine_similarity,
    document_content_fingerprint,
    document_content_hash,
)


def test_content_hash_prefers_body_across_different_urls_or_titles():
    body = "mesmo conteúdo jornalístico " * 30
    left = document_content_hash("Título A", "Resumo A", body)
    right = document_content_hash("Título B", "Resumo B", body)
    assert left == right


def test_hash_embedding_is_deterministic_and_normalized():
    first = _hash_embedding("Dossiê Mulher 2026 ISP Rio de Janeiro")
    second = _hash_embedding("Dossiê Mulher 2026 ISP Rio de Janeiro")
    assert first == second
    norm = sum(value * value for value in first) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_cosine_similarity_identical_vectors():
    vector = _hash_embedding("morte por intervenção de agente do Estado")
    assert cosine_similarity(vector, vector) > 0.999999


def test_content_fingerprint_is_stable_for_identical_body():
    body = "conteudo de reportagem sobre seguranca publica " * 25
    assert document_content_fingerprint("A", "x", body) == document_content_fingerprint("B", "y", body)

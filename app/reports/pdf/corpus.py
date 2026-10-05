from __future__ import annotations


def deduplicated_corpus(
    corpus: list[dict],
    by_origin: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Normaliza snapshots atuais e legados sem alterar o conteúdo persistido."""
    from app.services.metrics import deduplicate_corpus

    rows = list(corpus or [])
    if not rows and by_origin:
        rows = [
            *list(by_origin.get("redes_sociais") or []),
            *list(by_origin.get("youtube") or []),
            *list(by_origin.get("portal_noticias") or []),
        ]
    return deduplicate_corpus(rows)


def split_by_origin(corpus: list[dict]) -> dict[str, list[dict]]:
    """Import tardio para evitar ciclo com app.services."""
    from app.services.metrics import split_corpus_by_origin

    return split_corpus_by_origin(corpus or [])

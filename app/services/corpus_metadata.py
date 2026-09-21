"""Normalizacao deterministica dos metadados do corpus historico.

Esta camada corrige informacoes que podem ter sido persistidas antes da
classificacao de origem de midia e recupera datas apenas quando elas aparecem de
forma explicita em titulo/URL. Nao inventa datas parciais.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import unquote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CorpusDocument, MediaItem
from app.services.collection.media_origin import classify_media_origin


_DATE_PATTERNS = (
    re.compile(r"(?<!\\d)(20\\d{2})[\\/_-](0?[1-9]|1[0-2])[\\/_-](0?[1-9]|[12]\\d|3[01])(?!\\d)"),
    re.compile(r"(?<!\\d)(0?[1-9]|[12]\\d|3[01])[\\/_-](0?[1-9]|1[0-2])[\\/_-](20\\d{2})(?!\\d)"),
)


def infer_publication_date(*values: object) -> date | None:
    """Extrai somente datas completas e inequivocas presentes no texto."""
    for value in values:
        text = unquote(str(value or ""))
        if not text:
            continue

        match = _DATE_PATTERNS[0].search(text)
        if match:
            year, month, day = map(int, match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                pass

        match = _DATE_PATTERNS[1].search(text)
        if match:
            day, month, year = map(int, match.groups())
            try:
                return date(year, month, day)
            except ValueError:
                pass
    return None


def repair_corpus_metadata(db: Session, *, limit: int | None = None) -> dict[str, int]:
    """Reclassifica origem e preenche datas explicitas ausentes.

    Tambem propaga metadados melhores entre media_items e corpus_documents
    quando ambos apontam para o mesmo documento.
    """
    document_query = select(CorpusDocument).order_by(CorpusDocument.id.asc())
    item_query = select(MediaItem).order_by(MediaItem.id.asc())
    if limit:
        document_query = document_query.limit(limit)
        item_query = item_query.limit(limit)

    documents = list(db.scalars(document_query).all())
    items = list(db.scalars(item_query).all())
    documents_by_id = {document.id: document for document in documents}

    stats = {
        "documents_scanned": len(documents),
        "items_scanned": len(items),
        "origins_fixed": 0,
        "dates_inferred": 0,
        "dates_propagated": 0,
    }

    for document in documents:
        expected_origin = classify_media_origin(document.url, document.domain)
        if document.media_origin != expected_origin:
            document.media_origin = expected_origin
            stats["origins_fixed"] += 1

        if document.published_at is None:
            inferred = infer_publication_date(document.url, document.title)
            if inferred is not None:
                document.published_at = inferred
                stats["dates_inferred"] += 1

    for item in items:
        expected_origin = classify_media_origin(item.url, item.domain)
        if item.media_origin != expected_origin:
            item.media_origin = expected_origin
            stats["origins_fixed"] += 1

        if item.published_at is None:
            inferred = infer_publication_date(item.url, item.title)
            if inferred is not None:
                item.published_at = inferred
                stats["dates_inferred"] += 1

        document = documents_by_id.get(item.corpus_document_id)
        if document is None:
            continue

        if document.published_at is None and item.published_at is not None:
            document.published_at = item.published_at
            stats["dates_propagated"] += 1
        elif item.published_at is None and document.published_at is not None:
            item.published_at = document.published_at
            stats["dates_propagated"] += 1

        if document.media_origin != item.media_origin:
            document.media_origin = item.media_origin
            stats["origins_fixed"] += 1

    db.flush()
    return stats

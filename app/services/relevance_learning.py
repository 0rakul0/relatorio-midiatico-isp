"""Embeddings, supervised examples and a lightweight local relevance reranker.

The local reranker never decides final inclusion. It only prioritizes historical
corpus candidates; app.services.validation remains the authoritative relevance
check for every project.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import CorpusDocument, MediaItem, Project, RelevanceTrainingExample
from app.topic_profile import normalized_text


_STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma", "ao",
    "aos", "que", "sobre",
}


def _compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _document_text(
    title: str | None,
    snippet: str | None,
    content: str | None,
    *,
    max_chars: int | None = None,
) -> str:
    text = "\n\n".join(
        part for part in (_compact(title), _compact(snippet), _compact(content))
        if part
    )
    return text[:max_chars] if max_chars else text


def document_content_hash(
    title: str | None,
    snippet: str | None,
    content: str | None,
) -> str:
    """Stable SHA-256 that favors the article body for cross-URL deduplication."""
    body = _compact(content)
    if len(body) >= 160:
        basis = body
    else:
        basis = _document_text(title, snippet, content)
    return hashlib.sha256(
        normalized_text(basis).encode("utf-8", errors="ignore")
    ).hexdigest()


def document_content_fingerprint(
    title: str | None,
    snippet: str | None,
    content: str | None,
) -> str:
    """Fuzzy fingerprint used only to find cross-URL duplicate candidates."""
    body = _compact(content)
    basis = body if len(body) >= 160 else _document_text(title, snippet, content)
    tokens = re.findall(r"[a-z0-9]+", normalized_text(basis))
    if not tokens:
        return hashlib.sha256(b"").hexdigest()
    shingles = [
        " ".join(tokens[index:index + 5])
        for index in range(max(1, len(tokens) - 4))
    ]
    if len(tokens) < 5:
        shingles = [" ".join(tokens)]
    hashes = sorted(
        hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).hexdigest()
        for shingle in shingles
    )
    # A small bottom-k signature tolerates tracking/footer differences while
    # remaining deterministic and cheap to compare in SQL.
    return hashlib.sha256("|".join(hashes[:32]).encode("utf-8")).hexdigest()


def _hash_embedding(text: str, dimensions: int = 256) -> list[float]:
    vector = [0.0] * dimensions
    tokens = re.findall(r"[a-z0-9]+", normalized_text(text))
    features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big", signed=False)
        vector[raw % dimensions] += -1.0 if ((raw >> 8) & 1) else 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _desired_embedding_model() -> str:
    settings = get_settings()
    if (
        settings.corpus_embedding_provider.casefold() == "openai"
        and settings.openai_api_key
    ):
        return settings.corpus_embedding_model
    return "local-hash-v1"


def _embed_batch(texts: list[str]) -> tuple[list[list[float]], str]:
    settings = get_settings()
    if (
        settings.corpus_embedding_provider.casefold() == "openai"
        and settings.openai_api_key
    ):
        try:
            from openai import OpenAI

            kwargs = {"api_key": settings.openai_api_key}
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            client = OpenAI(**kwargs)
            rows: list[list[float]] = []
            batch_size = max(1, int(settings.corpus_embedding_batch_size))
            for offset in range(0, len(texts), batch_size):
                response = client.embeddings.create(
                    model=settings.corpus_embedding_model,
                    input=[
                        text[: settings.corpus_embedding_max_chars]
                        for text in texts[offset: offset + batch_size]
                    ],
                )
                rows.extend(list(item.embedding) for item in response.data)
            if len(rows) == len(texts):
                return rows, settings.corpus_embedding_model
        except Exception:
            if not settings.corpus_embedding_fallback_local:
                raise

    return [_hash_embedding(text) for text in texts], "local-hash-v1"


def cosine_similarity(
    left: list[float] | None,
    right: list[float] | None,
) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def project_embedding_text(project: Project) -> str:
    profile = project.topic_profile or {}
    values: list[str] = [project.topic]
    for key in (
        "product_name", "product_anchor", "event_anchor",
        "product_search_variants", "event_search_variants",
        "fact_discovery_variants", "subject_terms", "actors", "actions",
        "locations", "organizations", "search_synonyms",
    ):
        value = profile.get(key)
        if isinstance(value, list):
            values.extend(_compact(row) for row in value if _compact(row))
        elif value:
            values.append(_compact(value))
    return " | ".join(dict.fromkeys(value for value in values if value))


def backfill_content_hashes(db: Session, *, limit: int) -> int:
    documents = db.scalars(
        select(CorpusDocument)
        .where(CorpusDocument.content_hash.is_(None))
        .order_by(CorpusDocument.id.asc())
        .limit(max(1, limit))
    ).all()
    for document in documents:
        document.content_hash = document_content_hash(
            document.title, document.snippet, document.content
        )
        document.content_fingerprint = document_content_fingerprint(
            document.title, document.snippet, document.content
        )
        urls = list(document.alternate_urls or [])
        if document.url and document.url not in urls:
            urls.append(document.url)
        document.alternate_urls = urls[:50]
    return len(documents)


def _ensure_document_embeddings(
    db: Session,
    documents: list[CorpusDocument],
) -> str:
    desired = _desired_embedding_model()
    pending = [
        document for document in documents
        if not document.embedding or document.embedding_model != desired
    ]
    if pending:
        vectors, model_name = _embed_batch([
            _document_text(
                document.title,
                document.snippet,
                document.content,
                max_chars=get_settings().corpus_embedding_max_chars,
            )
            for document in pending
        ])
        now = datetime.now(timezone.utc)
        for document, vector in zip(pending, vectors):
            document.embedding = vector
            document.embedding_model = model_name
            document.embedded_at = now
        db.flush()
        return model_name
    return desired


def semantic_scores_for_documents(
    db: Session,
    project: Project,
    documents: list[CorpusDocument],
) -> dict[int, float]:
    if not documents:
        return {}
    _ensure_document_embeddings(db, documents)
    query_vectors, query_model = _embed_batch([project_embedding_text(project)])

    # If an external embedding call failed and fell back locally, make sure
    # document vectors use the same representation before comparing them.
    if any(document.embedding_model != query_model for document in documents):
        for document in documents:
            document.embedding = []
            document.embedding_model = None
            document.embedded_at = None
        _ensure_document_embeddings(db, documents)
        query_vectors, query_model = _embed_batch([project_embedding_text(project)])

    query_vector = query_vectors[0] if query_vectors else []
    return {
        document.id: max(0.0, cosine_similarity(query_vector, document.embedding))
        for document in documents
    }


def _tokens(value: str | None) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", normalized_text(value or ""))
        if len(token) > 2 and token not in _STOPWORDS
    }


def _pair_features(query_text: str, document_text: str) -> set[str]:
    query_tokens = _tokens(query_text)
    document_tokens = _tokens(document_text)
    overlap = query_tokens & document_tokens
    features = {f"ov:{token}" for token in overlap}
    features.update(f"d:{token}" for token in list(document_tokens)[:180])

    if query_tokens:
        coverage = len(overlap) / len(query_tokens)
        features.add(f"coverage:{min(9, int(coverage * 10))}")
    if document_tokens:
        precision = len(overlap) / min(50, len(document_tokens))
        features.add(f"precision:{min(9, int(precision * 10))}")
    return features


def record_relevance_training_examples(
    db: Session,
    project: Project,
    items: Iterable[MediaItem],
    *,
    decision_source: str,
) -> dict[str, int]:
    settings = get_settings()
    query_text = project_embedding_text(project)
    created = updated = 0
    for item in items:
        if item.status not in {"VALID", "NOT_RELATED"}:
            continue
        existing = db.scalar(
            select(RelevanceTrainingExample).where(
                RelevanceTrainingExample.project_id == project.id,
                RelevanceTrainingExample.media_item_id == item.id,
            )
        )
        values = {
            "corpus_document_id": item.corpus_document_id,
            "query_text": query_text,
            "document_title": item.title,
            "document_text": _document_text(
                item.title,
                item.snippet,
                item.content,
                max_chars=settings.relevance_training_max_chars,
            ),
            "label": 1 if item.status == "VALID" else 0,
            "relation_type": item.relation_type,
            "decision_source": decision_source,
            "decision_evidence": item.relevance_evidence or item.discard_reason,
        }
        if existing is None:
            db.add(RelevanceTrainingExample(
                project_id=project.id,
                media_item_id=item.id,
                **values,
            ))
            created += 1
        else:
            for key, value in values.items():
                setattr(existing, key, value)
            updated += 1
    db.flush()
    return {"created": created, "updated": updated}


def _model_path() -> Path:
    return Path(get_settings().reranker_model_path)


def _load_model() -> dict | None:
    path = _model_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def train_reranker(db: Session, *, force: bool = False) -> dict:
    settings = get_settings()
    examples = db.scalars(
        select(RelevanceTrainingExample)
        .order_by(RelevanceTrainingExample.id.asc())
    ).all()
    positives = [row for row in examples if row.label == 1]
    negatives = [row for row in examples if row.label == 0]

    if not force and (
        len(examples) < settings.reranker_min_training_examples
        or len(positives) < settings.reranker_min_examples_per_class
        or len(negatives) < settings.reranker_min_examples_per_class
    ):
        return {
            "trained": False,
            "reason": "insufficient_examples",
            "examples": len(examples),
            "positives": len(positives),
            "negatives": len(negatives),
        }

    pos_counts: Counter[str] = Counter()
    neg_counts: Counter[str] = Counter()
    for row in positives:
        pos_counts.update(_pair_features(row.query_text, row.document_text))
    for row in negatives:
        neg_counts.update(_pair_features(row.query_text, row.document_text))

    all_features = set(pos_counts) | set(neg_counts)
    weights: dict[str, float] = {}
    pos_n = max(1, len(positives))
    neg_n = max(1, len(negatives))
    for feature in all_features:
        pos_probability = (pos_counts[feature] + 1.0) / (pos_n + 2.0)
        neg_probability = (neg_counts[feature] + 1.0) / (neg_n + 2.0)
        weights[feature] = math.log(pos_probability / neg_probability)

    if len(weights) > settings.reranker_max_features:
        ranked = sorted(weights.items(), key=lambda row: abs(row[1]), reverse=True)
        weights = dict(ranked[: settings.reranker_max_features])

    model = {
        "version": 1,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "examples": len(examples),
        "positives": len(positives),
        "negatives": len(negatives),
        "bias": math.log((len(positives) + 1.0) / (len(negatives) + 1.0)),
        "weights": weights,
    }
    path = _model_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(model, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    except OSError as exc:
        return {
            "trained": False,
            "reason": "model_write_failed",
            "error": str(exc),
            "examples": len(examples),
        }
    return {
        "trained": True,
        "examples": len(examples),
        "positives": len(positives),
        "negatives": len(negatives),
        "features": len(weights),
        "model_path": str(path),
    }


def maybe_train_reranker(db: Session) -> dict:
    settings = get_settings()
    if not settings.reranker_auto_train:
        return {"trained": False, "reason": "disabled"}
    existing = _load_model()
    example_count = db.query(RelevanceTrainingExample).count()
    if (
        existing
        and example_count - int(existing.get("examples") or 0)
        < settings.reranker_retrain_delta
    ):
        return {
            "trained": False,
            "reason": "up_to_date",
            "examples": example_count,
        }
    return train_reranker(db, force=False)


def reranker_scores_for_documents(
    project: Project,
    documents: list[CorpusDocument],
) -> dict[int, float]:
    model = _load_model()
    if not model or not documents:
        return {}
    weights = model.get("weights") or {}
    bias = float(model.get("bias") or 0.0)
    query_text = project_embedding_text(project)
    scores: dict[int, float] = {}
    for document in documents:
        features = _pair_features(
            query_text,
            _document_text(document.title, document.snippet, document.content),
        )
        contributions = [float(weights.get(feature, 0.0)) for feature in features]
        raw = bias
        if contributions:
            raw += sum(contributions) / math.sqrt(len(contributions))
        raw = max(-20.0, min(20.0, raw))
        scores[document.id] = 1.0 / (1.0 + math.exp(-raw))
    return scores

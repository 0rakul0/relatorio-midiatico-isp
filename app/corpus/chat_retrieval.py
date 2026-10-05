from __future__ import annotations

import math
from datetime import date

from app.config import get_settings
from app.corpus.chat_text import tokens, topic_key
from app.models import MediaItem


def dedupe_items(items: list[MediaItem]) -> list[MediaItem]:
    unique: dict[str, MediaItem] = {}
    for item in items:
        key = (item.canonical_url or item.url or f"id:{item.id}").strip()
        current = unique.get(key)
        if current is None:
            unique[key] = item
            continue
        current_text = (current.content or current.snippet or "").strip()
        candidate_text = (item.content or item.snippet or "").strip()
        if len(candidate_text) > len(current_text):
            unique[key] = item
    return list(unique.values())


def recent_key(item: MediaItem) -> tuple[date, int]:
    return item.published_at or date.min, item.id or 0


def rank_items(
    items: list[MediaItem],
    question: str,
    limit: int,
) -> list[MediaItem]:
    if not items:
        return []

    query_tokens = tokens(question)
    if not query_tokens:
        return []

    normalized_question = topic_key(question)
    document_tokens: dict[int, set[str]] = {}
    document_frequency = {token: 0 for token in query_tokens}

    for item in items:
        combined = " ".join(
            filter(
                None,
                [item.title, item.snippet, (item.content or "")[:6000]],
            )
        )
        item_tokens = tokens(combined)
        document_tokens[item.id] = item_tokens
        for token in query_tokens:
            if token in item_tokens:
                document_frequency[token] += 1

    total = max(1, len(items))
    ranked: list[tuple[float, date, int, MediaItem]] = []
    for item in items:
        title_tokens = tokens(item.title)
        snippet_tokens = tokens(item.snippet)
        body_tokens = document_tokens.get(item.id, set())
        matched = query_tokens & body_tokens
        score = 0.0

        for token in matched:
            idf = math.log(
                (total + 1) / (document_frequency[token] + 1)
            ) + 1.0
            weight = 1.0
            if token in snippet_tokens:
                weight += 0.8
            if token in title_tokens:
                weight += 2.2
            score += idf * weight

        score += 4.0 * (len(matched) / len(query_tokens))

        haystack = topic_key(
            " ".join(
                filter(
                    None,
                    [item.title, item.snippet, (item.content or "")[:6000]],
                )
            )
        )
        if (
            len(normalized_question) >= 12
            and normalized_question in haystack
        ):
            score += 8.0

        published, item_id = recent_key(item)
        ranked.append((score, published, item_id, item))

    ranked.sort(
        key=lambda row: (row[0], row[1], row[2]),
        reverse=True,
    )
    selected = [row[3] for row in ranked if row[0] > 0][:limit]

    if len(selected) < min(6, limit):
        seen = {item.id for item in selected}
        for item in sorted(items, key=recent_key, reverse=True):
            if item.id in seen:
                continue
            selected.append(item)
            seen.add(item.id)
            if len(selected) >= limit:
                break
    return selected[:limit]


def serialize_members(items: list[MediaItem]) -> list[dict]:
    max_chars = get_settings().chat_item_max_chars
    members: list[dict] = []
    for index, item in enumerate(items):
        body = (item.content or "").strip() or (item.snippet or "").strip()
        members.append(
            {
                "index": index,
                "reference": f"F{index + 1}",
                "id": item.id,
                "title": item.title,
                "domain": item.domain,
                "url": item.url,
                "published_at": (
                    item.published_at.isoformat()
                    if item.published_at
                    else None
                ),
                "source_name": item.source_name,
                "media_origin": item.media_origin,
                "search_source": item.search_source,
                "content": body[:max_chars],
            }
        )
    return members

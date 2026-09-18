"""Persistência determinística dos resultados granulares de busca.

Os coletores em lote e os helpers de coleta chamam somente as capacidades de
``app.tools.search``. Este módulo é responsável por transformar as linhas
normalizadas retornadas pela capacidade em ``MediaItem`` auditáveis: guard,
janela temporal, domínio, canal prioritário, deduplicação e proveniência.

Nenhum provedor de pesquisa é acessado aqui.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import MediaItem, Project, SearchQuery
from app.services.collection.common import (
    _append_purpose,
    record_source_provenance,
    _site_domain_from_query,
    canonicalize,
    result_publication_date,
)
from app.services.collection.guards import collection_guard
from app.services.collection.youtube_helpers import is_youtube_url, matches_priority_youtube_channel


def persist_web_rows(
    db: Session,
    project: Project,
    query: SearchQuery | None,
    existing_items: dict[str, MediaItem],
    rows: list[dict[str, Any]],
    *,
    has_window: bool,
    start: date | None = None,
    end: date | None = None,
    max_accepted: int | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Persiste as linhas web retornadas pela capacidade de busca.

    Retorna ``(counts, accepted_rows)``. ``accepted_rows`` contém as linhas
    normalizadas que passaram pelo guard/janela (novas ou duplicadas), prontas
    para o retorno da tool. ``max_accepted`` limita quantos itens utilizáveis
    (novos ou duplicados) uma consulta pode consumir.
    """
    counters = {"returned": 0, "added": 0, "rejected": 0, "duplicates": 0}
    site_domain = _site_domain_from_query(query.query) if query else None
    is_youtube_query = bool(query and query.kind == "youtube")
    purpose = query.purpose if query else "MEDIA_REPERCUSSION"
    added_rows: list[dict[str, Any]] = []
    accepted = 0

    for row in rows:
        if max_accepted is not None and accepted >= max_accepted:
            break
        url = (row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            counters["rejected"] += 1
            continue

        host = urlparse(url).netloc.lower().split(":")[0]
        if site_domain and not (host == site_domain or host.endswith("." + site_domain)):
            counters["rejected"] += 1
            continue

        published_at = result_publication_date(row.get("published_at"))
        if has_window and published_at and not (start <= published_at <= end):
            counters["rejected"] += 1
            continue

        if is_youtube_query and not is_youtube_url(url):
            counters["rejected"] += 1
            continue

        counters["returned"] += 1
        snippet = row.get("snippet")
        content = row.get("content")
        guard_content = content or snippet or ""
        provider = str(row.get("provider") or "duckduckgo")

        if purpose == "MEDIA_REPERCUSSION" and not collection_guard(
            project,
            title=row.get("title"),
            snippet=snippet,
            content=guard_content,
        ):
            counters["rejected"] += 1
            continue

        canonical = canonicalize(url)
        source_name = row.get("source_name") or host
        existing = existing_items.get(canonical)
        if existing:
            counters["duplicates"] += 1
            accepted += 1
            _append_purpose(existing, purpose)
            record_source_provenance(
                existing,
                source=provider,
                title=row.get("title"),
                url=url,
                published_at=row.get("published_at"),
                snippet=snippet,
                channel=source_name,
                query=query.query if query else None,
            )
            if not existing.snippet and snippet:
                existing.snippet = snippet
            if not existing.content and content:
                existing.content = content
            if not existing.published_at and published_at:
                existing.published_at = published_at
            if not existing.source_name and source_name:
                existing.source_name = source_name
            added_rows.append(
                {
                    **row,
                    "source_name": source_name,
                    "provider": provider,
                }
            )
            continue

        row_obj = MediaItem(
            project_id=project.id,
            query_id=query.id if query else None,
            title=row.get("title") or "Sem titulo",
            url=url,
            canonical_url=canonical,
            domain=host,
            published_at=published_at,
            snippet=snippet,
            content=guard_content,
            source_name=source_name,
            search_source=provider,
            source_provenance=[
                {
                    "source": provider,
                    "title": row.get("title"),
                    "url": url,
                    "published_at": row.get("published_at"),
                    "snippet": snippet,
                    "channel": source_name,
                    "view_count": None,
                    "query": query.query if query else None,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            discovery_purposes=[purpose],
        )
        db.add(row_obj)
        existing_items[canonical] = row_obj
        counters["added"] += 1
        accepted += 1
        added_rows.append(
            {
                **row,
                "source_name": source_name,
                "provider": provider,
            }
        )

    if progress_detail:
        progress_detail(
            f"Coleta concluida: {counters['returned']} retornado(s), "
            f"{counters['added']} novo(s), {counters['duplicates']} duplicado(s), "
            f"{counters['rejected']} rejeitado(s)"
        )

    return counters, added_rows


def persist_video_rows(
    db: Session,
    project: Project,
    task,
    existing_items: dict[str, MediaItem],
    rows: list[dict[str, Any]],
    *,
    has_window: bool,
    start: date | None = None,
    end: date | None = None,
    max_accepted: int | None = None,
    enforce_priority_channel: bool = True,
    progress_detail: Callable[[str], None] | None = None,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Persiste as linhas de vídeo retornadas pela capacidade de busca.

    Aplica o auditor de canais prioritários, a janela de repercussão, o guard
    temático e a deduplicação por URL canônica do YouTube.
    """
    counters = {"returned": 0, "added": 0, "rejected": 0, "duplicates": 0}
    is_priority_task = task.is_priority
    added_rows: list[dict[str, Any]] = []
    accepted = 0

    for row in rows:
        if max_accepted is not None and accepted >= max_accepted:
            break
        url = (row.get("url") or "").strip()
        if not is_youtube_url(url):
            counters["rejected"] += 1
            continue

        channel = row.get("source_name")
        if is_priority_task and enforce_priority_channel and not matches_priority_youtube_channel(channel, task.target):
            counters["rejected"] += 1
            continue

        published_at = result_publication_date(row.get("published_at"))
        if has_window and published_at and not (start <= published_at <= end):
            counters["rejected"] += 1
            continue

        description = row.get("content") or row.get("snippet") or None
        title = row.get("title") or "Video sem titulo"
        if not collection_guard(
            project,
            title=title,
            snippet=description,
            content=description,
        ):
            counters["rejected"] += 1
            continue

        counters["returned"] += 1
        raw_view_count = row.get("view_count")
        view_count = (
            raw_view_count
            if isinstance(raw_view_count, int)
            and not isinstance(raw_view_count, bool)
            and raw_view_count >= 0
            else None
        )
        provider = str(row.get("provider") or "duckduckgo_video")

        canonical = canonicalize(url)
        existing = existing_items.get(canonical)
        if existing:
            counters["duplicates"] += 1
            accepted += 1
            record_source_provenance(
                existing,
                source=provider,
                title=title,
                url=url,
                published_at=row.get("published_at"),
                snippet=description,
                channel=channel,
                view_count=view_count,
                query=task.query,
                target=task.target,
            )
            existing.title = title or existing.title
            existing.published_at = published_at or existing.published_at
            existing.snippet = description or existing.snippet
            existing.content = description or existing.content
            existing.source_name = channel or existing.source_name
            existing.view_count = view_count if view_count is not None else existing.view_count
            existing.search_source = provider
            _append_purpose(existing, "MEDIA_REPERCUSSION")
            added_rows.append(
                {
                    **row,
                    "source_name": channel or existing.source_name or "YouTube",
                    "provider": provider,
                }
            )
            continue

        row_obj = MediaItem(
            project_id=project.id,
            title=title,
            url=url,
            canonical_url=canonical,
            domain=urlparse(url).netloc.lower(),
            published_at=published_at,
            snippet=description,
            content=description,
            source_name=channel or "YouTube",
            view_count=view_count,
            search_source=provider,
            source_provenance=[
                {
                    "source": provider,
                    "title": title,
                    "url": url,
                    "published_at": row.get("published_at"),
                    "snippet": description,
                    "channel": channel,
                    "view_count": view_count,
                    "query": task.query,
                    "target": task.target,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            discovery_purposes=["MEDIA_REPERCUSSION"],
        )
        db.add(row_obj)
        existing_items[canonical] = row_obj
        counters["added"] += 1
        accepted += 1
        added_rows.append(
            {
                **row,
                "source_name": channel or "YouTube",
                "provider": provider,
            }
        )

    if progress_detail:
        progress_detail(
            f"Coleta concluida: {counters['returned']} retornado(s), "
            f"{counters['added']} novo(s), {counters['duplicates']} duplicado(s), "
            f"{counters['rejected']} rejeitado(s)"
        )

    return counters, added_rows
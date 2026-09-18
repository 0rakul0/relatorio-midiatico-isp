from __future__ import annotations

import math
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.fact_layer import fact_events_for_main_report
from app.llm import llm_is_configured
from app.models import Classification, MediaItem, OfficialFact, Project
from app.schemas import MediaClassificationBatchResponse


def classify_with_llm(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    if not llm_is_configured():
        return {
            "updated": 0,
            "skipped": True,
            "already_classified": 0,
            "llm_calls": 0,
            "batches": 0,
        }

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    resolved_facts = fact_events_for_main_report(db, project.id)
    settings = get_settings()
    classification_cap = max(1, settings.max_classifications)
    batch_size = max(1, settings.classification_batch_size)
    max_chars = max(500, settings.classification_item_max_chars)

    items = db.scalars(
        select(MediaItem).where(
            MediaItem.project_id == project.id,
            MediaItem.status == "VALID",
        )
    ).all()

    classifications = {
        row.media_item_id: row
        for row in db.scalars(
            select(Classification).where(
                Classification.media_item_id.in_([item.id for item in items])
            )
        ).all()
    } if items else {}

    pending_items: list[MediaItem] = []
    already_classified = 0
    for item in items:
        classification = classifications.get(item.id)
        if classification and classification.fidelity_status != "PENDENTE":
            already_classified += 1
            continue
        pending_items.append(item)

    pending_after_quota = max(0, len(pending_items) - classification_cap)
    pending_items = pending_items[:classification_cap]

    official_facts = [
        {
            "label": fact.label,
            "value": fact.value,
            "evidence": fact.evidence,
            "indicator": fact.indicator,
            "geography": fact.geography,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat() if fact.period_end else None,
        }
        for fact in facts
    ]

    updated = 0
    llm_calls = 0
    failed_batches = 0
    total_batches = max(1, math.ceil(len(pending_items) / batch_size)) if pending_items else 0

    extra_instructions = """
Você receberá vários itens de mídia na mesma chamada. Classifique CADA item de forma independente.
Não use evidência de um item para classificar outro. Preserve exatamente o media_item_id recebido.
Retorne uma classificação para cada item apresentado. Se a evidência for insuficiente para qualquer
campo, use INVERIFICÁVEL em vez de preencher por inferência.
"""

    for batch_index, offset in enumerate(range(0, len(pending_items), batch_size), start=1):
        if cancel_check:
            cancel_check()
        batch = pending_items[offset : offset + batch_size]
        if progress_detail:
            progress_detail(
                f"Classificação em lote {batch_index}/{total_batches}: {len(batch)} item(ns)"
            )

        try:
            result = get_report_agent().run(
                task="classification",
                extra_instructions=extra_instructions,
                payload={
                    "official_facts": official_facts,
                    "resolved_facts": resolved_facts,
                    "media_items": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "url": item.url,
                            "snippet": item.snippet,
                            "content": (item.content or "")[:max_chars],
                        }
                        for item in batch
                    ],
                },
                schema_name="media_classification_batch_v1",
                response_model=MediaClassificationBatchResponse,
                max_output_tokens=max(2600, min(12000, 900 * len(batch))),
            )
            llm_calls += 1
        except RuntimeError:
            # Não transforma uma falha de lote em N chamadas individuais. Mantém
            # as classificações pendentes para uma reexecução futura e segue.
            llm_calls += 1
            failed_batches += 1
            continue

        by_id = {
            int(row["media_item_id"]): row
            for row in result.get("classifications", [])
            if row.get("media_item_id") is not None
        }

        for item in batch:
            row = by_id.get(item.id)
            if not row:
                continue
            classification = classifications.get(item.id)
            payload = {
                key: value
                for key, value in row.items()
                if key != "media_item_id"
            }
            if not classification:
                classification = Classification(media_item_id=item.id, **payload)
                db.add(classification)
                classifications[item.id] = classification
            else:
                for field, value in payload.items():
                    setattr(classification, field, value)
            updated += 1
        db.commit()

    return {
        "updated": updated,
        "skipped": False,
        "already_classified": already_classified,
        "pending_after_quota": pending_after_quota,
        "llm_calls": llm_calls,
        "batch_size": batch_size,
        "batches": total_batches,
        "failed_batches": failed_batches,
    }



from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    FactAssertion,
    FactEvent,
    MediaItem,
    OperationEvent,
    OperationMediaLink,
    Project,
)
from app.topic_profile import normalized_text


def operation_event_candidate(event: FactEvent) -> bool:
    haystack = " ".join(
        str(value or "")
        for value in (
            event.operation_name,
            event.event_type,
            event.circumstance,
            event.cause_description,
        )
    )
    return bool(event.operation_name) or "operac" in normalized_text(haystack)


def operation_display_name(event: FactEvent) -> str:
    if event.operation_name:
        return event.operation_name
    date_label = event.event_date.isoformat() if event.event_date else "data não identificada"
    place = event.neighborhood or event.city or "local não identificado"
    return f"Operação policial — {date_label} — {place}"


def sync_operation_events(db: Session, project: Project) -> dict:
    fact_events = db.scalars(
        select(FactEvent)
        .where(FactEvent.project_id == project.id)
        .order_by(FactEvent.event_date.asc().nullslast(), FactEvent.id.asc())
    ).all()
    operation_fact_ids = {event.id for event in fact_events if operation_event_candidate(event)}

    stale = db.scalars(select(OperationEvent).where(OperationEvent.project_id == project.id)).all()
    for row in stale:
        if row.fact_event_id not in operation_fact_ids:
            db.delete(row)
    db.flush()

    created = updated = 0
    for event in fact_events:
        if event.id not in operation_fact_ids:
            continue
        assertions = db.scalars(
            select(FactAssertion).where(FactAssertion.event_id == event.id)
        ).all()
        media_ids = sorted({int(a.media_item_id) for a in assertions if a.media_item_id is not None})
        media_items = (
            db.scalars(select(MediaItem).where(MediaItem.id.in_(media_ids))).all()
            if media_ids else []
        )
        item_by_id = {item.id: item for item in media_items}

        source_urls = sorted({a.source_url for a in assertions if a.source_url})
        official_urls = sorted({
            a.source_url for a in assertions
            if a.source_url and a.source_type == "OFFICIAL"
        })
        media_urls = sorted({
            a.source_url for a in assertions
            if a.source_url and a.source_type in {"MEDIA", "SOCIAL"}
        })
        valid_repercussion_ids = {item.id for item in media_items if item.status == "VALID"}
        forces = sorted({value for value in (event.institution, event.unit) if value})
        neighborhoods = sorted({
            value for value in (event.neighborhood, event.death_neighborhood) if value
        })

        row = db.scalar(
            select(OperationEvent).where(
                OperationEvent.project_id == project.id,
                OperationEvent.fact_event_id == event.id,
            )
        )
        if row is None:
            row = OperationEvent(project_id=project.id, fact_event_id=event.id)
            db.add(row)
            db.flush()
            created += 1
        else:
            updated += 1

        row.operation_name = operation_display_name(event)
        row.event_date = event.event_date or event.death_date
        row.city = event.city or event.death_city
        row.state = event.state or event.death_state
        row.neighborhoods = neighborhoods
        row.forces = forces
        row.resolution_status = event.resolution_status
        row.official_supported = bool(official_urls)
        row.official_source_count = len(official_urls)
        row.media_source_count = len(media_urls)
        row.repercussion_count = len(valid_repercussion_ids)
        row.source_urls = source_urls
        row.official_urls = official_urls
        row.media_urls = media_urls
        row.count_timelines = dict((event.extra_attributes or {}).get("count_timelines") or {})

        existing_links = {
            link.media_item_id: link
            for link in db.scalars(
                select(OperationMediaLink).where(OperationMediaLink.operation_event_id == row.id)
            ).all()
        }
        wanted_ids = set(media_ids)
        for media_item_id, link in existing_links.items():
            if media_item_id not in wanted_ids:
                db.delete(link)
        for media_item_id in wanted_ids:
            item = item_by_id.get(media_item_id)
            source_types = {
                a.source_type for a in assertions if a.media_item_id == media_item_id
            }
            source_type = (
                "OFFICIAL" if "OFFICIAL" in source_types
                else "SOCIAL" if "SOCIAL" in source_types
                else "MEDIA"
            )
            relation_type = "REPERCUSSION" if item and item.status == "VALID" else "EVIDENCE"
            link = existing_links.get(media_item_id)
            if link is None:
                db.add(OperationMediaLink(
                    operation_event_id=row.id,
                    media_item_id=media_item_id,
                    relation_type=relation_type,
                    source_type=source_type,
                ))
            else:
                link.relation_type = relation_type
                link.source_type = source_type

    db.flush()
    return {"operations": len(operation_fact_ids), "created": created, "updated": updated}


def operation_events_for_report(db: Session, project_id: int) -> list[dict]:
    rows = db.scalars(
        select(OperationEvent)
        .where(OperationEvent.project_id == project_id)
        .order_by(OperationEvent.event_date.asc().nullslast(), OperationEvent.id.asc())
    ).all()
    return [
        {
            "id": row.id,
            "fact_event_id": row.fact_event_id,
            "operation_name": row.operation_name,
            "event_date": row.event_date.isoformat() if row.event_date else None,
            "city": row.city,
            "state": row.state,
            "neighborhoods": row.neighborhoods or [],
            "forces": row.forces or [],
            "resolution_status": row.resolution_status,
            "official_supported": bool(row.official_supported),
            "official_source_count": int(row.official_source_count or 0),
            "media_source_count": int(row.media_source_count or 0),
            "repercussion_count": int(row.repercussion_count or 0),
            "source_urls": row.source_urls or [],
            "official_urls": row.official_urls or [],
            "media_urls": row.media_urls or [],
            "count_timelines": row.count_timelines or {},
        }
        for row in rows
    ]


def operation_mentions_for_report(db: Session, project_id: int) -> list[dict]:
    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.asc().nullslast(), MediaItem.id.asc())
    ).all()
    grouped: dict[tuple[str, str], dict] = {}
    named_pattern = re.compile(
        r"\b(opera(?:ç|c)[aã]o\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][^:;|—–-]{1,80})",
        flags=re.IGNORECASE,
    )
    for item in items:
        haystack = " ".join([item.title or "", item.snippet or ""])
        if "operac" not in normalized_text(haystack):
            continue
        match = named_pattern.search(item.title or "")
        label = " ".join(match.group(1).split()).strip() if match else (item.title or "Operação policial citada")
        event_date = item.published_at.isoformat() if item.published_at else None
        key = (event_date or "sem-data", normalized_text(label))
        row = grouped.setdefault(key, {
            "id": None,
            "fact_event_id": None,
            "operation_name": label,
            "event_date": event_date,
            "city": None,
            "state": "RJ",
            "neighborhoods": [],
            "forces": [],
            "resolution_status": "PROVISIONAL_MEDIA_MENTION",
            "inventory_status": "PROVISIONAL_MEDIA_MENTION",
            "official_supported": False,
            "official_source_count": 0,
            "media_source_count": 0,
            "repercussion_count": 0,
            "source_urls": [],
            "official_urls": [],
            "media_urls": [],
            "count_timelines": {},
        })
        if item.url and item.url not in row["media_urls"]:
            row["media_urls"].append(item.url)
            row["source_urls"].append(item.url)
        row["media_source_count"] = len(row["media_urls"])
        row["repercussion_count"] = len(row["media_urls"])
    return list(grouped.values())


def operation_inventory_summary(db: Session, project: Project) -> dict:
    operations = operation_events_for_report(db, project.id)
    if not project.event_start or not project.event_end:
        return {
            "operations": len(operations),
            "months_expected": 0,
            "months_with_operations": [],
            "months_with_official_support": [],
            "months_missing_operations": [],
            "months_missing_official_support": [],
            "coverage_ratio": None,
            "official_coverage_ratio": None,
        }

    expected: list[str] = []
    year, month = project.event_start.year, project.event_start.month
    while (year, month) <= (project.event_end.year, project.event_end.month):
        expected.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1

    months_with_operations = sorted({
        str(row.get("event_date"))[:7] for row in operations if row.get("event_date")
    })
    months_with_official_support = sorted({
        str(row.get("event_date"))[:7]
        for row in operations if row.get("event_date") and row.get("official_supported")
    })
    missing_operations = [m for m in expected if m not in months_with_operations]
    missing_official = [m for m in expected if m not in months_with_official_support]
    total = len(expected)
    return {
        "operations": len(operations),
        "official_supported_operations": sum(bool(row.get("official_supported")) for row in operations),
        "operations_with_media_repercussion": sum(int(row.get("repercussion_count") or 0) > 0 for row in operations),
        "months_expected": total,
        "months_with_operations": months_with_operations,
        "months_with_official_support": months_with_official_support,
        "months_missing_operations": missing_operations,
        "months_missing_official_support": missing_official,
        "coverage_ratio": round(len(months_with_operations) / total, 3) if total else None,
        "official_coverage_ratio": round(len(months_with_official_support) / total, 3) if total else None,
    }

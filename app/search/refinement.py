from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project, SearchQuery
from app.search.inventory import (
    PT_MONTHS,
    annual_event_inventory_queries,
    official_operation_inventory_queries,
)
from app.search.persistence import add_query
from app.source_registry import OFFICIAL_OPERATION_INVENTORY_SOURCES
from app.topic_profile import normalized_text


def _annual_event_inventory_queries(project: Project) -> list[tuple[str, str]]:
    return annual_event_inventory_queries(project, settings=get_settings())


def _official_operation_inventory_queries(project: Project) -> list[tuple[str, str, str]]:
    return official_operation_inventory_queries(project, settings=get_settings())


_PT_MONTHS = PT_MONTHS
_add_query = add_query


def plan_missing_month_operation_inventory_queries(
    db: Session,
    project: Project,
    months: list[str],
) -> list[SearchQuery]:
    """Retorna consultas mensais pendentes e cria variantes quando necessario."""
    wanted = {
        str(value)
        for value in months
        if isinstance(value, str) and len(value) == 7 and value[4] == "-"
    }
    if not wanted or not project.event_start or not project.event_end:
        return []

    existing_rows = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project.id)
        .order_by(SearchQuery.id.asc())
    ).all()
    existing = {row.query for row in existing_rows}
    ready: list[SearchQuery] = []

    def month_key_for_query(query: str) -> str | None:
        normalized_query = normalized_text(query)
        for month_index, month_name in enumerate(_PT_MONTHS, start=1):
            if month_name not in normalized_query:
                continue
            for year in range(project.event_start.year, project.event_end.year + 1):
                if str(year) in normalized_query:
                    return f"{year:04d}-{month_index:02d}"
        return None

    # Reaproveita consultas mensais ja existentes e ainda nao executadas.
    for row in existing_rows:
        if (
            row.executed_at is None
            and row.kind in {"fact_inventory_month", "official_operation_inventory"}
            and month_key_for_query(row.query) in wanted
        ):
            ready.append(row)

    # Cria consultas-base ausentes.
    for query, rationale in _annual_event_inventory_queries(project):
        if month_key_for_query(query) not in wanted:
            continue
        row = _add_query(
            db, project, existing,
            query=query,
            kind="fact_inventory_month",
            purpose="FACT_DISCOVERY",
            rationale=f"[refinamento inventario] {rationale}",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

    for query, kind, rationale in _official_operation_inventory_queries(project):
        if month_key_for_query(query) not in wanted:
            continue
        row = _add_query(
            db, project, existing,
            query=query,
            kind=kind,
            purpose="OFFICIAL_FACT",
            rationale=f"[refinamento inventario] {rationale}",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

    # Se as consultas-base ja foram executadas e o mes continua vazio,
    # abre variantes de recall sem apagar a trilha das tentativas anteriores.
    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "operacoes policiais").strip()
    locations = profile.get("locations") or ["Rio de Janeiro"]
    location = str(locations[0] or "Rio de Janeiro")

    for month_key in sorted(wanted):
        year = int(month_key[:4])
        month_index = int(month_key[5:7])
        month_name = _PT_MONTHS[month_index - 1]
        existing_for_month = [
            row for row in existing_rows
            if month_key_for_query(row.query) == month_key
            and row.kind in {"fact_inventory_month", "official_operation_inventory"}
        ]
        if any(row.executed_at is None for row in existing_for_month):
            continue

        attempts = len(existing_for_month)
        open_variants = [
            f'"{anchor}" "{location}" {month_name} {year} balanco operacao',
            f'"operacao policial" "{location}" {month_name} {year} mortos presos apreensoes',
            f'"operacoes policiais" RJ {month_name} {year} policia civil militar',
        ]
        open_query = open_variants[min(attempts, len(open_variants) - 1)]
        row = _add_query(
            db, project, existing,
            query=open_query,
            kind="fact_inventory_month",
            purpose="FACT_DISCOVERY",
            rationale=f"[refinamento inventario] Variante de recall para {month_name}/{year}.",
            priority=1,
        )
        if row:
            db.flush()
            ready.append(row)

        for source in OFFICIAL_OPERATION_INVENTORY_SOURCES:
            domain = source["domain"]
            official_variants = [
                f'site:{domain} operacao {month_name} {year} Rio de Janeiro',
                f'site:{domain} "operacao policial" {month_name} {year}',
                f'site:{domain} operacao balanco {month_name} {year}',
            ]
            query = official_variants[min(attempts, len(official_variants) - 1)]
            row = _add_query(
                db, project, existing,
                query=query,
                kind="official_operation_inventory",
                purpose="OFFICIAL_FACT",
                rationale=(
                    f"[refinamento inventario] Variante oficial {source['label']} "
                    f"para {month_name}/{year}."
                ),
                priority=1,
            )
            if row:
                db.flush()
                ready.append(row)

    db.commit()
    unique: list[SearchQuery] = []
    seen: set[int] = set()
    for row in ready:
        marker = int(row.id or 0)
        if marker and marker in seen:
            continue
        if marker:
            seen.add(marker)
        unique.append(row)
    return unique


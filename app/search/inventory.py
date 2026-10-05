from __future__ import annotations

from app.config import get_settings
from app.models import Project
from app.source_registry import OFFICIAL_OPERATION_INVENTORY_SOURCES
from app.topic_profile import normalized_text


PT_MONTHS = [
    "janeiro", "fevereiro", "marco", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def annual_event_inventory_queries(project: Project) -> list[tuple[str, str]]:
    """Varredura mensal para pautas anuais de operações/eventos recorrentes."""
    settings = get_settings()
    if not settings.enable_annual_event_inventory:
        return []
    if project.project_type != "EVENT_TOPIC" or not project.event_start or not project.event_end:
        return []
    if (project.event_end - project.event_start).days < 180:
        return []

    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "").strip()
    anchor_norm = normalized_text(anchor)
    topic_norm = normalized_text(project.topic)
    if not ("operac" in anchor_norm or "operac" in topic_norm):
        return []
    if not ("polic" in anchor_norm or "polic" in topic_norm):
        return []

    locations = profile.get("locations") or ["Rio de Janeiro"]
    location = str(locations[0] or "Rio de Janeiro")
    start = project.event_start
    end = project.event_end
    queries: list[tuple[str, str]] = []
    year_month = (start.year, start.month)
    while year_month <= (end.year, end.month):
        year, month = year_month
        month_name = PT_MONTHS[month - 1]
        query = f'"{anchor}" "{location}" {month_name} {year}'
        queries.append((query, f"Inventário mensal de operações policiais: {month_name}/{year}."))
        if len(queries) >= settings.max_annual_event_inventory_queries:
            break
        month += 1
        if month == 13:
            year += 1
            month = 1
        year_month = (year, month)
    return queries


def official_operation_inventory_queries(project: Project) -> list[tuple[str, str, str]]:
    """Descoberta mensal em fontes primárias para inventários anuais."""
    settings = get_settings()
    if not settings.enable_official_annual_inventory:
        return []
    if not project.event_start or not project.event_end:
        return []
    if (project.event_end - project.event_start).days < 180:
        return []

    profile = project.topic_profile or {}
    anchor = str(profile.get("event_anchor") or project.topic or "").strip()
    normalized = normalized_text(anchor + " " + project.topic)
    if "operac" not in normalized or "polic" not in normalized:
        return []

    rows: list[tuple[str, str, str]] = []
    year_month = (project.event_start.year, project.event_start.month)
    while year_month <= (project.event_end.year, project.event_end.month):
        year, month = year_month
        month_name = PT_MONTHS[month - 1]
        for source in OFFICIAL_OPERATION_INVENTORY_SOURCES:
            query = f'site:{source["domain"]} operacao policial {month_name} {year}'
            rows.append((
                query,
                "official_operation_inventory",
                f'Inventário oficial mensal de operações: {source["label"]}, {month_name}/{year}.',
            ))
            if len(rows) >= settings.max_official_inventory_queries:
                return rows
        month += 1
        if month == 13:
            year += 1
            month = 1
        year_month = (year, month)
    return rows

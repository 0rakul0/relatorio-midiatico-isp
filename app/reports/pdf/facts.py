from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.helpers import scope_label, status_label
from app.reports.pdf.table import table
from app.utils.rendering import has_content, text


def add_fact_section(
    story: list,
    *,
    facts: list[dict],
    report: dict,
    heading,
    body,
    small,
) -> None:
    if not facts:
        return

    story.append(Paragraph("Camada de Fatos Verificados", heading))
    if has_content(report.get("fact_layer_intro")):
        story.append(Paragraph(escape(text(report.get("fact_layer_intro"))), body))

    rows = [["Pessoa", "Vínculo", "Data", "Fato / causa", "Local do fato", "Local da morte", "Situação"]]
    for event in facts:
        link = " / ".join(
            value
            for value in [event.get("institution"), event.get("rank_or_role"), event.get("unit")]
            if value
        )
        location = ", ".join(
            value
            for value in [
                event.get("address"),
                event.get("neighborhood"),
                event.get("city"),
                event.get("state"),
            ]
            if value
        )
        death_location = ", ".join(
            value
            for value in [
                event.get("death_place_name"),
                event.get("death_address"),
                event.get("death_neighborhood"),
                event.get("death_city"),
                event.get("death_state"),
            ]
            if value
        )
        cause = event.get("cause") or event.get("circumstance") or "Não localizado"
        status = status_label(event.get("resolution_status"))
        if event.get("conflict_fields"):
            status += " - " + ", ".join(event["conflict_fields"])
        status += f"; {scope_label(event.get('primary_scope'))}"
        rows.append([
            event.get("subject_name") or "Não localizado",
            link or "Não localizado",
            event.get("death_date") or event.get("event_date") or "Não localizada",
            cause,
            location or "Não localizado",
            death_location or "Não localizado",
            status,
        ])
    story.append(
        table(
            rows,
            [2.3 * cm, 2.5 * cm, 1.6 * cm, 2.8 * cm, 2.7 * cm, 2.7 * cm, 2.0 * cm],
            small,
        )
    )

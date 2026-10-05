from __future__ import annotations

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.table import table


def add_operations_section(
    story: list,
    *,
    operations: list[dict],
    heading,
    small,
) -> None:
    if not operations:
        return

    provisional_operations = any(
        operation.get("inventory_status") == "PROVISIONAL_MEDIA_MENTION"
        or operation.get("resolution_status") == "PROVISIONAL_MEDIA_MENTION"
        for operation in operations
    )
    story.append(
        Paragraph(
            "Operações citadas na amostra (inventário provisório)"
            if provisional_operations
            else "Inventário de operações identificadas",
            heading,
        )
    )
    story.append(
        Paragraph(
            (
                "A camada factual estruturada ainda não individualizou as operações; esta tabela é um fallback auditável "
                "construído apenas a partir de itens validados que citam operações."
                if provisional_operations
                else "Cada linha representa uma operação/evento individualizado na camada factual. Links oficiais sustentam "
                "a identificação factual e links de mídia apontam a repercussão associada localizada."
            ),
            small,
        )
    )
    op_rows = [["Mês", "Data", "Operação", "Local", "Força(s)", "Matérias", "Links"]]
    for operation in operations:
        event_date = str(operation.get("event_date") or "")
        month = event_date[:7] if len(event_date) >= 7 else "N/D"
        location_values = [
            *(operation.get("neighborhoods") or []),
            operation.get("city"),
            operation.get("state"),
        ]
        location = ", ".join(value for value in location_values if value) or "N/D"
        forces = " / ".join(operation.get("forces") or []) or "N/D"
        links = []
        for index, url in enumerate(operation.get("official_urls") or [], start=1):
            links.append({"href": str(url), "label": f"Oficial {index}"})
        for index, url in enumerate(operation.get("media_urls") or [], start=1):
            links.append({"href": str(url), "label": f"Mídia {index}"})
        op_rows.append([
            month,
            event_date or "N/D",
            operation.get("operation_name") or "Operação não nomeada",
            location,
            forces,
            str(operation.get("repercussion_count") or 0),
            {"_pdf_links": links},
        ])
    story.append(
        table(
            op_rows,
            [1.25 * cm, 1.55 * cm, 3.2 * cm, 3.0 * cm, 2.5 * cm, 1.2 * cm, 4.0 * cm],
            small,
        )
    )

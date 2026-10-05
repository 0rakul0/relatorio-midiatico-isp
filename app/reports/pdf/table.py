from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle

from app.utils.rendering import text


def table(
    rows: list[list[object]],
    widths: list[float],
    style,
    header_color=None,
    header_text=None,
    nowrap_columns: set[int] | None = None,
):
    header_style = ParagraphStyle(
        "TableHeader",
        parent=style,
        textColor=header_text if header_text is not None else colors.white,
    )
    nowrap_columns = set(nowrap_columns or set())

    def cell_paragraph(cell: object, cell_style, row_index: int, column_index: int):
        if isinstance(cell, dict) and "_pdf_links" in cell:
            parts = []
            for link_item in cell.get("_pdf_links") or []:
                href = text((link_item or {}).get("href")).strip()
                label = escape(text((link_item or {}).get("label") or "Abrir"))
                if not href:
                    continue
                safe_href = escape(href, {'"': '&quot;', "'": '&apos;'})
                parts.append(
                    f'<link href="{safe_href}" color="#1F5E8C"><u>{label}</u></link>'
                )
            return Paragraph(" · ".join(parts) if parts else "N/D", cell_style)
        if isinstance(cell, dict) and "_pdf_link" in cell:
            href = text(cell.get("_pdf_link")).strip()
            label = escape(text(cell.get("label") or "Abrir"))
            if not href:
                return Paragraph("N/D", cell_style)
            safe_href = escape(href, {'"': '&quot;', "'": '&apos;'})
            return Paragraph(
                f'<link href="{safe_href}" color="#1F5E8C"><u>{label}</u></link>',
                cell_style,
            )
        if row_index > 0 and column_index in nowrap_columns:
            return text(cell)
        return Paragraph(escape(text(cell)), cell_style)

    formatted = [
        [
            cell_paragraph(
                cell,
                header_style if row_index == 0 else style,
                row_index,
                column_index,
            )
            for column_index, cell in enumerate(row)
        ]
        for row_index, row in enumerate(rows)
    ]
    result = Table(
        formatted,
        colWidths=widths,
        repeatRows=1,
        splitByRow=1,
    )
    result.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), header_color or colors.HexColor("#102b46")),
                ("TEXTCOLOR", (0, 0), (-1, 0), header_text or colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe4ec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 5),
                ("FONTNAME", (0, 1), (-1, -1), getattr(style, "fontName", "Helvetica")),
                ("FONTSIZE", (0, 1), (-1, -1), getattr(style, "fontSize", 7.2)),
            ]
        )
    )
    return result

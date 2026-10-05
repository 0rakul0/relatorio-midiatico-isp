from __future__ import annotations

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.helpers import pdf_link
from app.reports.pdf.table import table
from app.utils.rendering import text


def add_academic_section(
    story: list,
    *,
    academic_papers: list[dict],
    heading,
    small,
) -> None:
    if not academic_papers:
        return

    story.append(Paragraph("Literatura científica relacionada", heading))
    story.append(
        Paragraph(
            "Trabalhos recuperados em bases acadêmicas para contextualização científica. "
            "Eles não são contados como repercussão midiática e não confirmam "
            "automaticamente fatos noticiados.",
            small,
        )
    )
    rows = [["Ano", "Autores", "Artigo", "Relação com o tema", "Fonte"]]
    for paper in academic_papers:
        authors = list(paper.get("authors") or [])
        author_text = ", ".join(authors[:4]) + (" et al." if len(authors) > 4 else "")
        published = text(paper.get("published_at"))
        rows.append([
            published[:4] if published else "N/D",
            author_text or "N/D",
            (
                (paper.get("title") or "Sem título")
                + (
                    "\nOriginal: " + str(paper.get("title_original"))
                    if paper.get("title_original")
                    and paper.get("title_original") != paper.get("title")
                    else ""
                )
            ),
            paper.get("relation_to_topic") or "Contexto científico relacionado",
            pdf_link(paper.get("url")),
        ])
    story.append(
        table(
            rows,
            [1.0 * cm, 3.2 * cm, 4.1 * cm, 5.4 * cm, 2.9 * cm],
            small,
        )
    )

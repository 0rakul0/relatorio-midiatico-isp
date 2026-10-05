from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.helpers import pdf_link
from app.reports.pdf.table import table
from app.utils.rendering import has_content, text


def _corpus_table(rows: list[dict], small):
    if not rows:
        return Paragraph(
            "Nenhum item validado nesta categoria para a janela observada.",
            small,
        )
    return table(
        [["#", "Data", "Fonte", "Título", "Link"]]
        + [
            [
                str(index + 1),
                item.get("published_at") or item.get("published_year", "N/D"),
                item.get("source") or item.get("domain") or "Fonte aberta",
                (
                    item.get("title")
                    + (
                        f" (+{int(item.get('duplicate_count') or 0)} duplicata(s) consolidada(s))"
                        if int(item.get("duplicate_count") or 0)
                        else ""
                    )
                ),
                pdf_link(item.get("url")),
            ]
            for index, item in enumerate(rows)
        ],
        [0.9 * cm, 2.3 * cm, 3.0 * cm, 9.2 * cm, 2.2 * cm],
        small,
        nowrap_columns={0, 1},
    )


def add_audit_annexes(
    story: list,
    *,
    report: dict,
    fact_layer_enabled: bool,
    fact_evidence: list,
    social_post_inventory: list[dict],
    social_items: list[dict],
    youtube_items: list[dict],
    portal_items: list[dict],
    heading,
    body,
    small,
) -> None:
    annex_sections: list[tuple[str, object, str]] = []
    methodological_note = report.get("methodological_note")
    if has_content(methodological_note):
        annex_sections.append(("Nota Metodológica", methodological_note, "methodology"))
    if fact_layer_enabled and fact_evidence:
        annex_sections.append(("Evidências Factuais", fact_evidence, "facts"))
    if social_post_inventory:
        annex_sections.append(
            ("Mídias Sociais Monitoradas", social_post_inventory, "social_posts")
        )
    elif social_items:
        annex_sections.append(("Mídias Sociais", social_items, "corpus"))
    for annex_name, rows in (
        ("YouTube", youtube_items),
        ("Portais de Notícias", portal_items),
    ):
        if rows:
            annex_sections.append((annex_name, rows, "corpus"))

    if annex_sections:
        story.append(Paragraph("ANEXOS AUDITÁVEIS", heading))

    for offset, (annex_name, content, annex_type) in enumerate(annex_sections):
        letter = chr(ord("A") + offset)
        story.append(Paragraph(f"Anexo {letter} - {annex_name}", heading))

        if annex_type == "methodology":
            story.append(
                Paragraph(
                    escape(text(content)).replace("\n", "<br/>"),
                    body,
                )
            )
            continue

        if annex_type == "social_posts":
            story.append(
                Paragraph(
                    "Posts públicos usados como âncoras da camada social. Estes itens são auditáveis "
                    "separadamente e não são somados ao corpus jornalístico validado.",
                    small,
                )
            )
            story.append(
                table(
                    [["#", "Plataforma", "Data", "Post / referência", "Comentários", "Descoberta", "Link"]]
                    + [
                        [
                            index + 1,
                            item.get("platform") or "N/D",
                            item.get("published_at") or "N/D",
                            item.get("title") or "Post social",
                            int(item.get("comments_collected") or 0),
                            item.get("discovery_source") or "monitoramento social",
                            pdf_link(item.get("url")),
                        ]
                        for index, item in enumerate(content)
                    ],
                    [0.7 * cm, 1.8 * cm, 1.8 * cm, 6.1 * cm, 1.5 * cm, 2.4 * cm, 2.3 * cm],
                    small,
                    nowrap_columns={0},
                )
            )
            continue

        if annex_type == "facts":
            story.append(
                Paragraph(
                    "Cada linha preserva o campo, valor, fonte, evidência textual e link usados na camada factual.",
                    small,
                )
            )
            story.append(
                table(
                    [["Pessoa", "Campo", "Valor", "Fonte", "Evidência", "Link"]]
                    + [
                        [
                            item.get("subject_name") or "N/D",
                            item.get("field") or "N/D",
                            item.get("value") or "N/D",
                            item.get("source") or item.get("source_type") or "N/D",
                            item.get("evidence") or "N/D",
                            pdf_link(item.get("url")),
                        ]
                        for item in content
                    ],
                    [2.2 * cm, 1.8 * cm, 2.4 * cm, 2.4 * cm, 6.0 * cm, 1.8 * cm],
                    small,
                )
            )
            continue

        rows = list(content)
        collapsed = sum(int(item.get("duplicate_count") or 0) for item in rows)
        note = "Itens validados na janela de repercussão."
        if collapsed:
            note += f" {collapsed} entrada(s) duplicada(s) foram consolidadas neste anexo."
        story.append(Paragraph(note, small))
        story.append(_corpus_table(rows, small))

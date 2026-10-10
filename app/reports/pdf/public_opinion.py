from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.helpers import pdf_link
from app.reports.pdf.table import table
from app.utils.rendering import text


def add_public_opinion_section(
    story: list,
    *,
    report: dict,
    public_opinion: dict,
    heading,
    body,
    small,
) -> None:
    surveys = list(public_opinion.get("surveys") or [])
    if not surveys:
        return

    story.append(Paragraph("Opinião pública sobre o tema", heading))
    public_summary = report.get("public_opinion_summary")
    if public_summary:
        story.append(Paragraph(escape(text(public_summary)), body))
    story.append(
        Paragraph(
            "Esta camada reúne levantamentos amostrais identificados separadamente da cobertura jornalística e dos comentários em redes sociais. "
            "Os resultados devem ser lidos dentro do universo pesquisado, do período de campo e das limitações de cada estudo.",
            small,
        )
    )

    survey_rows = [["Instituto", "Campo", "População / amostra", "Metodologia", "Fonte"]]
    for survey in surveys:
        field_start = text(survey.get("field_start")).strip()
        field_end = text(survey.get("field_end")).strip()
        field_label = (
            f"{field_start} a {field_end}"
            if field_start and field_end
            else field_start or field_end or "N/D"
        )
        population = text(survey.get("population")).strip() or "N/D"
        sample_size = survey.get("sample_size")
        if sample_size:
            population += f" · n={sample_size}"
        method_bits = [
            text(survey.get("sampling_method")).strip(),
            text(survey.get("margin_of_error")).strip(),
            text(survey.get("confidence_level")).strip(),
        ]
        method_label = (
            " · ".join(bit for bit in method_bits if bit)
            or text(survey.get("methodology")).strip()
            or "N/D"
        )
        survey_rows.append([
            survey.get("institute") or "N/D",
            field_label,
            population,
            method_label,
            pdf_link(survey.get("source_url")),
        ])
    story.append(
        table(
            survey_rows,
            [3.0 * cm, 3.1 * cm, 4.8 * cm, 3.8 * cm, 1.9 * cm],
            small,
        )
    )

    indicator_rows = [["Instituto", "Indicador / pergunta", "Resultado", "Recorte"]]
    seen_indicators: set[tuple[str, str, str, str]] = set()
    for survey in surveys:
        for indicator in survey.get("indicators") or []:
            question = indicator.get("question") or indicator.get("label") or "Indicador"
            value = text(indicator.get("value")).strip()
            unit = text(indicator.get("unit")).strip()
            result_label = (value + (f" {unit}" if unit else "")).strip() or "N/D"
            subgroup = indicator.get("subgroup") or survey.get("representative_scope") or "Total da amostra"
            key = (text(survey.get("institute")).casefold(), text(question).casefold(), result_label.casefold(), text(subgroup).casefold())
            if key in seen_indicators:
                continue
            seen_indicators.add(key)
            indicator_rows.append([survey.get("institute") or "N/D", question, result_label, subgroup])
    if len(indicator_rows) > 1:
        story.append(Paragraph("Indicadores de opinião pública - síntese", heading))
        if len(indicator_rows) > 19:
            story.append(Paragraph(f"Seleção de 18 entre {len(indicator_rows)-1} indicadores. O conjunto integral permanece na base auditável.", small))
        story.append(
            table(
                indicator_rows[:19],
                [3.0 * cm, 6.6 * cm, 2.8 * cm, 4.2 * cm],
                small,
            )
        )

    methodology_note = public_opinion.get("methodology_note")
    if methodology_note:
        story.append(
            Paragraph(
                "<b>Nota metodológica:</b> " + escape(text(methodology_note)),
                small,
            )
        )

from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.table import table
from app.utils.rendering import text


def _social_label(value: object) -> str:
    labels = {
        "instagram": "Instagram",
        "facebook": "Facebook",
        "x": "X",
        "POSITIVO": "Positivo",
        "NEGATIVO": "Negativo",
        "NEUTRO": "Neutro",
        "AMBIGUO": "Ambíguo",
        "MEDO": "Medo",
        "INDIGNACAO": "Indignação",
        "CONFIANCA": "Confiança",
        "DESCONFIANCA": "Desconfiança",
        "TRISTEZA": "Tristeza",
        "IRONIA": "Ironia",
        "ESPERANCA": "Esperança",
        "OUTRA": "Outra",
        "NAO_IDENTIFICAVEL": "Não identificável",
        "APOIO": "Apoio",
        "CRITICA": "Crítica",
        "PREOCUPACAO": "Preocupação",
        "DUVIDA": "Dúvida",
        "RELATO_PESSOAL": "Relato pessoal",
    }
    raw = text(value)
    return labels.get(raw, raw.replace("_", " ").title())


def add_social_section(
    story: list,
    *,
    social_repercussion: dict,
    heading,
    body,
    small,
) -> None:
    social_comments = int(social_repercussion.get("comments") or 0)
    social_analyzed = int(social_repercussion.get("analyzed_comments") or 0)
    social_views = int(social_repercussion.get("view_count_total") or 0)
    social_view_posts = int(social_repercussion.get("view_count_known_posts") or 0)
    if not social_comments and not int(social_repercussion.get("posts") or 0):
        return

    story.append(Paragraph("Percepção observada nas redes sociais", heading))
    if social_repercussion.get("summary"):
        story.append(Paragraph(escape(text(social_repercussion.get("summary"))), body))

    story.append(
        table(
            [
                ["Posts sociais", "Comentários coletados", "Comentários analisados", "Visualizações disponíveis"],
                [
                    social_repercussion.get("posts", 0),
                    social_comments,
                    social_analyzed,
                    social_views if social_view_posts else "N/D",
                ],
            ],
            [3.8 * cm, 4.3 * cm, 4.3 * cm, 4.2 * cm],
            small,
        )
    )

    platform_counts = social_repercussion.get("platform_counts") or {}
    platform_views = social_repercussion.get("platform_view_counts") or {}
    if platform_counts or platform_views:
        story.append(Paragraph("Distribuição por plataforma", heading))
        labels = sorted(
            set(platform_counts) | set(platform_views),
            key=lambda label: int(platform_views.get(label, 0) or 0),
            reverse=True,
        )
        story.append(
            table(
                [["Plataforma", "Comentários", "Visualizações disponíveis"]]
                + [
                    [
                        _social_label(label),
                        int(platform_counts.get(label, 0) or 0),
                        int(platform_views[label]) if label in platform_views else "N/D",
                    ]
                    for label in labels
                ],
                [6.0 * cm, 4.6 * cm, 6.0 * cm],
                small,
            )
        )

    if social_view_posts:
        story.append(
            Paragraph(
                "<b>Alcance observado:</b> "
                + escape(text(social_repercussion.get("view_count_note") or "")),
                small,
            )
        )

    def add_distribution(title: str, values: dict) -> None:
        if not values or not social_analyzed:
            return
        rows = []
        for label, count in sorted(
            values.items(),
            key=lambda item: int(item[1] or 0),
            reverse=True,
        ):
            count_value = int(count or 0)
            percent = count_value * 100.0 / social_analyzed if social_analyzed else 0
            rows.append([
                _social_label(label),
                count_value,
                f"{percent:.1f}%".replace(".", ","),
            ])
        story.append(Paragraph(title, heading))
        story.append(
            table(
                [["Classificação", "Comentários", "Participação na amostra"]] + rows,
                [7.0 * cm, 4.0 * cm, 5.6 * cm],
                small,
            )
        )

    add_distribution("Sentimento observado", social_repercussion.get("sentiment_counts") or {})
    add_distribution("Emoções observadas", social_repercussion.get("emotion_counts") or {})
    add_distribution("Posição em relação ao tema", social_repercussion.get("position_counts") or {})

    themes = list(social_repercussion.get("themes") or [])
    if themes:
        story.append(Paragraph("Temas recorrentes nos comentários", heading))
        story.append(
            table(
                [["#", "Tema", "Ocorrências"]]
                + [
                    [index + 1, item.get("theme") or "N/D", item.get("count") or 0]
                    for index, item in enumerate(themes)
                ],
                [1.4 * cm, 12.6 * cm, 2.6 * cm],
                small,
            )
        )

    discourse = social_repercussion.get("discourse_analysis") or {}
    if discourse:
        story.append(Paragraph("Leitura qualitativa dos comentários", heading))
        if discourse.get("overall_reading"):
            story.append(Paragraph(escape(text(discourse.get("overall_reading"))), body))

        def add_discourse_group(title: str, rows: list[dict]) -> None:
            cleaned = [
                row
                for row in (rows or [])
                if isinstance(row, dict) and (row.get("title") or row.get("analysis"))
            ]
            if not cleaned:
                return
            story.append(Paragraph(title, heading))
            for row in cleaned:
                label = escape(text(row.get("title") or "Achado"))
                analysis_text = escape(text(row.get("analysis") or ""))
                story.append(Paragraph(f"<b>{label}</b> - {analysis_text}", body))

        add_discourse_group("Narrativas dominantes", discourse.get("dominant_narratives") or [])
        add_discourse_group("Argumentos recorrentes", discourse.get("recurring_arguments") or [])
        add_discourse_group("Tensões e contradições", discourse.get("tensions_and_contradictions") or [])
        add_discourse_group("Formas de interação", discourse.get("interaction_patterns") or [])
        if discourse.get("polarization_signals"):
            story.append(Paragraph("Sinais de polarização na amostra", heading))
            story.append(Paragraph(escape(text(discourse.get("polarization_signals"))), body))
        if discourse.get("sample_limitations"):
            story.append(Paragraph("Limitações da leitura social", heading))
            story.append(Paragraph(escape(text(discourse.get("sample_limitations"))), small))

    methodology_note = social_repercussion.get("methodology_note")
    if methodology_note:
        story.append(
            Paragraph(
                "<b>Nota metodológica:</b> " + escape(text(methodology_note)),
                small,
            )
        )

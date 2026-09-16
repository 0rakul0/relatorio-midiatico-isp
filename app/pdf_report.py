from io import BytesIO
from xml.sax.saxutils import escape


def _text(value: object) -> str:
    return (
        str(value or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
    )


def _scope_label(value: object) -> str:
    if value is True:
        return "núcleo principal"
    if value is False:
        return "caso relacionado"
    return "escopo pendente"


def _status_label(value: str | None) -> str:
    labels = {
        "CONFIRMED": "Confirmado",
        "PARTIALLY_CONFIRMED": "Confirmação parcial",
        "SOURCE_CONFLICT": "Conflito entre fontes",
        "NOT_FOUND_IN_SAMPLE": "Não localizado na amostra",
    }
    return labels.get(value or "", value or "N/D")


def build_pdf(data: dict) -> bytes:
    """Cria o PDF a partir do relatório persistido, sem chamar LLM novamente."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    report = data["report"]
    project = data["project"]
    metrics = data["metrics"]
    corpus = data.get("corpus", [])
    facts = data.get("fact_events", [])
    fact_evidence = data.get("fact_evidence", [])
    traditional_corpus = data.get("traditional_corpus", [])
    social_corpus = data.get("social_corpus", [])
    qa = data.get("qa", {})

    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=19,
        leading=23,
        textColor=colors.HexColor("#102b46"),
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#516579"),
        spaceAfter=14,
    )
    heading = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#102b46"),
        spaceBefore=15,
        spaceAfter=7,
    )
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.2, leading=13, spaceAfter=8)
    small = ParagraphStyle("Small", parent=body, fontSize=7.2, leading=9)

    buffer = BytesIO()

    def page_number(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#66788a"))
        canvas.drawString(2 * cm, 1.15 * cm, "Relatório de Repercussão Midiática - corpus auditável")
        canvas.drawRightString(A4[0] - 2 * cm, 1.15 * cm, f"Página {doc.page}")
        canvas.restoreState()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.7 * cm,
        leftMargin=1.7 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.8 * cm,
    )

    story = [
        Paragraph("RELATÓRIO DE REPERCUSSÃO MIDIÁTICA", small),
        Paragraph(escape(_text(report["title"])), title),
        Paragraph(
            escape(_text(report["interpretive_title"])),
            ParagraphStyle("Interpretive", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14),
        ),
        Paragraph(escape(_text(report["subtitle"])), subtitle),
    ]

    if project.get("project_type") == "INSTITUTIONAL_PRODUCT":
        contextual_meta = "<b>Lançamento</b><br/>" + escape(_text(project.get("launch_date")))
    else:
        contextual_meta = (
            "<b>Janela dos fatos</b><br/>"
            + escape(_text(project.get("event_start")))
            + " a "
            + escape(_text(project.get("event_end")))
        )

    metadata = [
        [
            Paragraph("<b>Instituição</b><br/>" + escape(_text(project.get("institution"))), small),
            Paragraph(contextual_meta, small),
        ],
        [
            Paragraph(
                "<b>Janela de repercussão</b><br/>"
                + escape(_text(project.get("collection_start")))
                + " a "
                + escape(_text(project.get("collection_end"))),
                small,
            ),
            Paragraph(
                f"<b>Corpus validado</b><br/>{metrics.get('valid_items', 0)} itens em {metrics.get('unique_vehicles', 0)} veículos",
                small,
            ),
        ],
    ]
    if qa:
        metadata.append(
            [
                Paragraph("<b>Tipo de projeto</b><br/>" + escape(_text(project.get("project_type"))), small),
                Paragraph("<b>QA final</b><br/>" + escape(_text(qa.get("status", "N/D"))), small),
            ]
        )

    meta_table = Table(metadata, colWidths=[8.3 * cm, 8.3 * cm])
    meta_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f6fa")),
                ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#dbe4ec")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#dbe4ec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story += [meta_table, Spacer(1, 10)]

    def add_section(name: str, content: str):
        story.append(
            KeepTogether(
                [
                    Paragraph(name, heading),
                    Paragraph(escape(_text(content)).replace("\n", "<br/>"), body),
                ]
            )
        )

    add_section("Resumo Executivo", report["executive_summary"])

    story.append(Paragraph("Camada de Fatos Verificados", heading))
    story.append(Paragraph(escape(_text(report.get("fact_layer_intro", ""))), body))
    if facts:
        rows = [["Pessoa", "Vínculo", "Data", "Fato / causa", "Local do fato", "Local da morte", "Situação"]]
        for event in facts:
            link = " / ".join(
                value for value in [event.get("institution"), event.get("rank_or_role"), event.get("unit")] if value
            )
            location = ", ".join(
                value
                for value in [event.get("address"), event.get("neighborhood"), event.get("city"), event.get("state")]
                if value
            )
            death_location = ", ".join(
                value
                for value in [
                    event.get("death_place_name"), event.get("death_address"),
                    event.get("death_neighborhood"), event.get("death_city"), event.get("death_state")
                ]
                if value
            )
            cause = event.get("cause") or event.get("circumstance") or "Não localizado"
            status = _status_label(event.get("resolution_status"))
            if event.get("conflict_fields"):
                status += " - " + ", ".join(event["conflict_fields"])
            status += f"; {_scope_label(event.get('primary_scope'))}"
            rows.append(
                [
                    event.get("subject_name") or "Não localizado",
                    link or "Não localizado",
                    event.get("death_date") or event.get("event_date") or "Não localizada",
                    cause,
                    location or "Não localizado",
                    death_location or "Não localizado",
                    status,
                ]
            )
        story.append(_table(rows, [2.3 * cm, 2.5 * cm, 1.6 * cm, 2.8 * cm, 2.7 * cm, 2.7 * cm, 2.0 * cm], small))
    else:
        story.append(Paragraph("Nenhum fato individual foi suficientemente estruturado na amostra factual.", small))

    add_section("Abertura", report["opening"])
    add_section("I. Panorama da Repercussão", report["panorama"])

    thematic_fronts = len(report.get("thematic_axes", []))
    metric_content = [
        Paragraph(
            "REPERCUSSÃO POR NÚMEROS",
            ParagraphStyle("MetricHeading", parent=heading, fontSize=11, leading=14, textColor=colors.black, spaceBefore=8),
        )
    ]
    number_lines = [
        f"<b>{metrics.get('valid_items', 0)} itens distintos verificados</b> na amostra auditável;",
        f"<b>{metrics.get('unique_vehicles', 0)} veículos distintos</b> com cobertura auditável;",
        f"<b>{thematic_fronts} frentes temáticas</b> sintetizadas no relatório;",
        f"<b>{metrics.get('discarded_items', 0)} itens descartados</b> por insuficiência de relação documental;",
        f"<b>{metrics.get('collection_days', 0)} dia(s)</b> na janela de observação;",
        f"<b>{metrics.get('isp_mentioned_items', 0)} itens ({metrics.get('isp_protagonism_percent', 0)}%)</b> mencionam a instituição na amostra.",
    ]
    metric_content.extend([Paragraph("• " + line, body) for line in number_lines])
    scout = metrics.get("media_scout", {})
    if scout:
        platforms = " · ".join(
            f"{item.get('platform')}: {item.get('status')}"
            for item in scout.get("platforms", [])
        )
        metric_content.append(
            Paragraph(
                f"<b>{escape(_text(scout.get('name', 'Agente de monitoramento de veículos')))}</b> - "
                f"{scout.get('web_tasks', 0)} consultas em sites e {scout.get('youtube_tasks', 0)} no YouTube planejadas. "
                f"{escape(_text(platforms))}",
                small,
            )
        )
    youtube_note = metrics.get("youtube_collection_note")
    if youtube_note:
        metric_content.append(Paragraph(f"<b>Nota sobre YouTube:</b> {escape(_text(youtube_note))}", small))
    story.append(KeepTogether(metric_content))

    portal_checks = metrics.get("portal_checks", [])
    if portal_checks:
        story.append(Paragraph("CHECAGEM DE PORTAIS PRIORITÁRIOS", heading))
        story.append(
            _table(
                [["Portal", "Resultado", "Evidência"]]
                + [[item["portal"], item["result"], item["evidence"]] for item in portal_checks],
                [2.6 * cm, 4.2 * cm, 9.8 * cm],
                small,
                header_color=colors.HexColor("#f0f0f0"),
                header_text=colors.black,
            )
        )

    priority_channels = metrics.get("youtube_priority_channel_checks", [])
    if priority_channels:
        story.append(
            KeepTogether(
                [
                    Paragraph("CHECAGEM DE CANAIS PRIORITÁRIOS NO YOUTUBE", heading),
                    _table(
                        [["Canal", "Resultado", "Vídeos", "Visualizações", "Link do vídeo de maior alcance"]]
                        + [
                            [
                                item.get("channel", "N/D"),
                                item.get("result", "N/D"),
                                item.get("videos", 0),
                                f"{item.get('views', 0):,}".replace(",", "."),
                                item.get("lead_url", ""),
                            ]
                            for item in priority_channels
                        ],
                        [2.4 * cm, 3.3 * cm, 1.5 * cm, 2.2 * cm, 7.2 * cm],
                        small,
                        header_color=colors.HexColor("#f0f0f0"),
                        header_text=colors.black,
                    ),
                ]
            )
        )

    top_channels = metrics.get("top_youtube_channels", [])
    if top_channels:
        story.append(
            KeepTogether(
                [
                    Paragraph("TOP 5 CANAIS NO YOUTUBE", heading),
                    _table(
                        [["#", "Canal", "Vídeos validados", "Visualizações", "Link do vídeo de maior alcance"]]
                        + [
                            [
                                index + 1,
                                item.get("channel", "N/D"),
                                item.get("videos", 0),
                                f"{item.get('views', 0):,}".replace(",", "."),
                                item.get("lead_url", ""),
                            ]
                            for index, item in enumerate(top_channels)
                        ],
                        [0.7 * cm, 3.0 * cm, 2.3 * cm, 2.2 * cm, 8.4 * cm],
                        small,
                    ),
                ]
            )
        )

    add_section("II. Enquadramento Dominante", report["dominant_framing"])
    story.append(Paragraph("III. Um Estudo, Muitas Pautas", heading))
    story.append(
        _table(
            [["Eixo temático", "Dado-âncora", "Cobertura"]]
            + [[x["axis"], x["anchor_data"], x["coverage"]] for x in report["thematic_axes"]],
            [4.1 * cm, 5.7 * cm, 6.8 * cm],
            small,
        )
    )
    add_section("IV. Recorte de Maior Rendimento Jornalístico", report["highest_yield"])
    add_section("V. Camada Institucional e Disputa de Narrativa", report["institutional_narrative"])

    story.append(Paragraph("VI. Avaliação: Alcance, Profundidade e Riscos", heading))
    story.append(
        _table(
            [["Dimensão", "Avaliação", "Evidência"]]
            + [[x["dimension"], x["assessment"], x["evidence"]] for x in report["risk_assessment"]],
            [3.6 * cm, 4.7 * cm, 8.3 * cm],
            small,
        )
    )

    story.append(Paragraph("VII. Recomendações e Kit de Imprensa", heading))
    story.extend([Paragraph("• " + escape(_text(item)), body) for item in report["recommendations"]])
    story.append(
        _table(
            [["Produto", "Finalidade"]] + [[x["product"], x["purpose"]] for x in report["press_kit"]],
            [6.5 * cm, 10.1 * cm],
            small,
        )
    )
    add_section("VIII. Síntese", report["synthesis"])
    add_section("Anexo A - Nota Metodológica", report["methodological_note"])

    story.append(PageBreak())
    story.append(Paragraph("Anexo B - Evidências Factuais", heading))
    story.append(
        Paragraph(
            "Cada linha preserva o campo, valor, fonte, evidência textual e URL usados na camada factual.",
            small,
        )
    )
    if fact_evidence:
        story.append(
            _table(
                [["Pessoa", "Campo", "Valor", "Fonte", "Evidência", "URL"]]
                + [
                    [
                        item.get("subject_name") or "N/D",
                        item.get("field") or "N/D",
                        item.get("value") or "N/D",
                        item.get("source") or item.get("source_type") or "N/D",
                        item.get("evidence") or "N/D",
                        item.get("url") or "",
                    ]
                    for item in fact_evidence
                ],
                [2.2 * cm, 1.8 * cm, 2.4 * cm, 2.2 * cm, 4.5 * cm, 3.5 * cm],
                small,
            )
        )
    else:
        story.append(Paragraph("Nenhuma evidência factual estruturada nesta versão.", small))

    def corpus_table(rows):
        if not rows:
            return Paragraph("Nenhum item validado nesta categoria para a janela observada.", small)
        return _table(
            [["#", "Data", "Fonte", "Título", "URL"]]
            + [
                [
                    str(index + 1),
                    item.get("published_at") or item.get("published_year", "N/D"),
                    item.get("source") or item.get("domain") or "Fonte aberta",
                    item.get("title"),
                    item.get("url"),
                ]
                for index, item in enumerate(rows)
            ],
            [0.75 * cm, 1.6 * cm, 2.3 * cm, 5.2 * cm, 6.75 * cm],
            small,
        )

    story.append(PageBreak())
    story.append(Paragraph("Anexo C - Sites Tradicionais", heading))
    story.append(Paragraph("Itens validados na janela de repercussão.", small))
    story.append(corpus_table(traditional_corpus))

    story.append(PageBreak())
    story.append(Paragraph("Anexo D - Mídias Sociais e Plataformas", heading))
    story.append(Paragraph("Itens validados na janela de repercussão.", small))
    story.append(corpus_table(social_corpus))

    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buffer.getvalue()


def _table(rows: list[list[object]], widths: list[float], style, header_color=None, header_text=None):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle

    header_style = ParagraphStyle(
        "TableHeader",
        parent=style,
        textColor=header_text if header_text is not None else colors.white,
    )
    formatted = [
        [
            Paragraph(escape(_text(cell)), header_style if row_index == 0 else style)
            for cell in row
        ]
        for row_index, row in enumerate(rows)
    ]
    table = Table(formatted, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), header_color or colors.HexColor("#102b46")),
                ("TEXTCOLOR", (0, 0), (-1, 0), header_text or colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe4ec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    return table

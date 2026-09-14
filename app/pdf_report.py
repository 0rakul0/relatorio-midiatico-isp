from io import BytesIO
from xml.sax.saxutils import escape


def _text(value: object) -> str:
    return str(value or "").replace("—", "-").replace("–", "-").replace("“", '"').replace("”", '"').replace("’", "'")


def build_pdf(data: dict) -> bytes:
    """Cria um PDF A4 do relatório já persistido, sem chamar a LLM novamente."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    report, project, metrics, corpus = data["report"], data["project"], data["metrics"], data["corpus"]
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=23, textColor=colors.HexColor("#102b46"), spaceAfter=8)
    subtitle = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=10, leading=14, textColor=colors.HexColor("#516579"), spaceAfter=14)
    heading = ParagraphStyle("Section", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=colors.HexColor("#102b46"), spaceBefore=15, spaceAfter=7)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.2, leading=13, spaceAfter=8)
    small = ParagraphStyle("Small", parent=body, fontSize=7.2, leading=9)
    buffer = BytesIO()

    def page_number(canvas, doc):
        canvas.saveState(); canvas.setFont("Helvetica", 7); canvas.setFillColor(colors.HexColor("#66788a"))
        canvas.drawString(2 * cm, 1.15 * cm, "Relatório de Repercussão Midiática - corpus auditável")
        canvas.drawRightString(A4[0] - 2 * cm, 1.15 * cm, f"Página {doc.page}"); canvas.restoreState()

    document = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=1.7 * cm, leftMargin=1.7 * cm, topMargin=1.6 * cm, bottomMargin=1.8 * cm)
    story = [Paragraph("RELATÓRIO DE REPERCUSSÃO MIDIÁTICA", small), Paragraph(escape(_text(report["title"])), title),
             Paragraph(escape(_text(report["interpretive_title"])), ParagraphStyle("Interpretive", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14)),
             Paragraph(escape(_text(report["subtitle"])), subtitle)]
    metadata = [[Paragraph("<b>Instituição</b><br/>" + escape(_text(project["institution"])), small), Paragraph("<b>Lançamento</b><br/>" + escape(_text(project["launch_date"])), small)],
                [Paragraph("<b>Janela observada</b><br/>" + escape(_text(project["collection_start"])) + " a " + escape(_text(project["collection_end"])), small), Paragraph(f"<b>Corpus validado</b><br/>{metrics['valid_items']} itens em {metrics['unique_vehicles']} veículos", small)]]
    meta_table = Table(metadata, colWidths=[8.3 * cm, 8.3 * cm]); meta_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f6fa")), ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dbe4ec")), ("INNERGRID", (0, 0), (-1, -1), .3, colors.HexColor("#dbe4ec")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 7)])); story += [meta_table, Spacer(1, 10)]

    def add_section(name: str, content: str):
        story.append(KeepTogether([Paragraph(name, heading), Paragraph(escape(_text(content)).replace("\n", "<br/>"), body)]))

    add_section("Resumo Executivo", report["executive_summary"]); add_section("Abertura", report["opening"]); add_section("I. Panorama da Repercussão", report["panorama"])
    metric_content = [Paragraph("REPERCUSSÃO POR NÚMEROS", ParagraphStyle("MetricHeading", parent=heading, fontSize=11, leading=14, textColor=colors.black, spaceBefore=8))]
    thematic_fronts = len(report.get("thematic_axes", []))
    number_lines = [
        f"<b>{metrics['valid_items']} itens distintos verificados</b> na amostra auditável;",
        f"<b>{metrics['unique_vehicles']} veículos distintos</b> com cobertura auditável;",
        f"<b>{thematic_fronts} frentes temáticas</b> sintetizadas no relatório;",
        f"<b>{metrics['discarded_items']} itens descartados</b> por insuficiência de relação documental;",
        f"<b>{metrics.get('collection_days', 0)} dia(s)</b> na janela de observação;",
        f"<b>{metrics.get('isp_mentioned_items', 0)} itens ({metrics.get('isp_protagonism_percent', 0)}%)</b> mencionam a instituição na amostra.",
    ]
    metric_content.extend([Paragraph("• " + item, body) for item in number_lines])
    portal_checks = metrics.get("portal_checks", [])
    if portal_checks:
        metric_content.append(Paragraph("CHECAGEM DE PORTAIS PRIORITÁRIOS", ParagraphStyle("PortalHeading", parent=heading, fontSize=11, leading=14, textColor=colors.black, spaceBefore=12)))
        metric_content.append(_table([["Portal", "Resultado", "Evidência"]] + [[item["portal"], item["result"], item["evidence"]] for item in portal_checks], [2.6 * cm, 4.3 * cm, 9.7 * cm], small, header_color=colors.HexColor("#f0f0f0"), header_text=colors.black))
    story.append(KeepTogether(metric_content))
    add_section("II. Enquadramento Dominante", report["dominant_framing"])
    story.append(Paragraph("III. Um Estudo, Muitas Pautas", heading)); story.append(_table([["Eixo temático", "Dado-âncora", "Cobertura"]] + [[x["axis"], x["anchor_data"], x["coverage"]] for x in report["thematic_axes"]], [4.1 * cm, 5.7 * cm, 6.8 * cm], small))
    add_section("IV. Recorte de Maior Rendimento Jornalístico", report["highest_yield"]); add_section("V. Camada Institucional e Disputa de Narrativa", report["institutional_narrative"])
    story.append(Paragraph("VI. Avaliação: Alcance, Profundidade e Riscos", heading)); story.append(_table([["Dimensão", "Avaliação", "Evidência"]] + [[x["dimension"], x["assessment"], x["evidence"]] for x in report["risk_assessment"]], [3.6 * cm, 4.7 * cm, 8.3 * cm], small))
    story.append(Paragraph("VII. Recomendações e Kit de Imprensa", heading)); story.extend([Paragraph("• " + escape(_text(item)), body) for item in report["recommendations"]]); story.append(_table([["Produto", "Finalidade"]] + [[x["product"], x["purpose"]] for x in report["press_kit"]], [6.5 * cm, 10.1 * cm], small))
    add_section("VIII. Síntese", report["synthesis"]); add_section("Anexo A - Nota Metodológica", report["methodological_note"])
    story.append(PageBreak()); story.append(Paragraph("Anexo B - Corpus Auditável", heading)); story.append(Paragraph("A tabela reúne somente itens validados para análise, com ano de publicação, fonte, título e URL.", small)); story.append(_table([["#", "Ano", "Fonte", "Título", "URL"]] + [[str(i + 1), x.get("published_year", "Não informado"), x.get("source", x["domain"] or "Fonte aberta"), x["title"], x["url"]] for i, x in enumerate(corpus)], [0.55 * cm, 1.15 * cm, 2.35 * cm, 5.25 * cm, 7.0 * cm], small))
    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buffer.getvalue()


def _table(rows: list[list[object]], widths: list[float], style, header_color=None, header_text=None):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Table, TableStyle
    header_style = ParagraphStyle("TableHeader", parent=style, textColor=header_text if header_text is not None else colors.white)
    formatted = [[Paragraph(escape(_text(cell)), header_style if row_index == 0 else style) for cell in row] for row_index, row in enumerate(rows)]
    table = Table(formatted, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), header_color or colors.HexColor("#102b46")), ("TEXTCOLOR", (0, 0), (-1, 0), header_text or colors.white), ("GRID", (0, 0), (-1, -1), .25, colors.HexColor("#dbe4ec")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 5)]))
    return table

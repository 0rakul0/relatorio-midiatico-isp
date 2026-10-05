from io import BytesIO
from xml.sax.saxutils import escape

from app.reports.pdf.corpus import (
    deduplicated_corpus as _deduplicated_corpus,
    split_by_origin as _split_by_origin,
)
from app.reports.pdf.helpers import (
    pdf_link as _pdf_link,
    scope_label as _scope_label,
    status_label as _status_label,
    view_count_label as _view_count_label,
    window_label as _window_label,
)
from app.reports.pdf.word_cloud import pdf_word_cloud_flowable as _pdf_word_cloud_flowable
from app.reports.pdf.table import table as _table
from app.reports.pdf.public_opinion import add_public_opinion_section
from app.reports.pdf.social import add_social_section
from app.utils.rendering import (
    has_content as _has_content,
    has_meaningful_fields as _has_meaningful_fields,
    text as _text,
)


def build_pdf(data: dict) -> bytes:
    """Cria o PDF a partir do relatório persistido, sem chamar LLM novamente."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    report = data["report"]
    project = data["project"]
    metrics = dict(data["metrics"])
    corpus = _deduplicated_corpus(
        data.get("corpus", []),
        data.get("corpus_by_origin") or {},
    )
    facts = [
        item for item in (data.get("fact_events", []) or [])
        if _has_meaningful_fields(
            item,
            (
                "subject_name", "institution", "rank_or_role", "unit",
                "event_date", "death_date", "cause", "circumstance",
                "address", "neighborhood", "city", "state",
                "death_place_name", "death_address", "death_neighborhood",
                "death_city", "death_state",
            ),
        )
    ]
    operations = [
        item for item in (data.get("operation_events", []) or [])
        if _has_meaningful_fields(
            item,
            (
                "operation_name", "event_date", "neighborhoods", "city", "state",
                "forces", "official_urls", "media_urls", "repercussion_count",
            ),
        )
    ]
    fact_evidence = data.get("fact_evidence", [])
    academic_papers = [
        item for item in (data.get("academic_papers", []) or [])
        if _has_meaningful_fields(
            item,
            ("title", "title_original", "authors", "published_at", "relation_to_topic", "url"),
        )
    ]
    social_repercussion = data.get("social_repercussion") or {}
    public_opinion = data.get("public_opinion") or {}
    public_opinion_surveys = list(public_opinion.get("surveys") or [])
    social_post_inventory = list(social_repercussion.get("post_inventory") or [])
    word_cloud = data.get("word_cloud") or {}
    by_origin = _split_by_origin(corpus)
    social_items = by_origin.get("redes_sociais", [])
    youtube_items = by_origin.get("youtube", [])
    portal_items = by_origin.get("portal_noticias", [])
    metrics["valid_items"] = len(corpus)
    metrics["duplicates_collapsed"] = sum(
        int(item.get("duplicate_count") or 0) for item in corpus
    )
    execution_flags = project.get("execution_flags") or {}
    fact_layer_enabled = bool(execution_flags.get("enable_fact_layer"))

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
        # Evita título órfão no rodapé. O título acompanha o próximo
        # flowable, mas o conteúdo seguinte continua livre para quebrar
        # naturalmente entre páginas quando for longo.
        keepWithNext=True,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=9.2,
        leading=13,
        spaceAfter=8,
        alignment=TA_JUSTIFY,
    )
    # Metadados, notas curtas e tabelas continuam alinhados à esquerda para
    # evitar espaçamento excessivo em colunas estreitas.
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=7.2,
        leading=9,
        alignment=TA_LEFT,
    )
    cloud_note_style = ParagraphStyle(
        "WordCloudNote",
        parent=small,
        alignment=1,
        textColor=colors.HexColor("#66788a"),
        spaceBefore=2,
    )

    class LeftAccentParagraph(Flowable):
        """Parágrafo quebrável com barra vertical azul à esquerda."""

        def __init__(
            self,
            text: str,
            paragraph_style,
            *,
            bar_color: str = "#0879bd",
            bar_width: float = 2.2,
            gap: float = 9.0,
        ):
            super().__init__()
            self.text = text
            self.paragraph_style = paragraph_style
            self.bar_color = bar_color
            self.bar_width = bar_width
            self.gap = gap
            self.paragraph = Paragraph(text, paragraph_style)
            self._content_width = 0.0

        def wrap(self, avail_width, avail_height):
            self._content_width = max(1.0, avail_width - self.bar_width - self.gap)
            _, height = self.paragraph.wrap(self._content_width, avail_height)
            self.width = avail_width
            self.height = height
            return avail_width, height

        def split(self, avail_width, avail_height):
            content_width = max(1.0, avail_width - self.bar_width - self.gap)
            parts = self.paragraph.split(content_width, avail_height)
            if len(parts) <= 1:
                return []

            flowables = []
            for part in parts:
                block = LeftAccentParagraph(
                    "",
                    self.paragraph_style,
                    bar_color=self.bar_color,
                    bar_width=self.bar_width,
                    gap=self.gap,
                )
                block.paragraph = part
                flowables.append(block)
            return flowables

        def draw(self):
            canvas = self.canv
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor(self.bar_color))
            canvas.setLineWidth(self.bar_width)
            x = self.bar_width / 2.0
            canvas.line(x, 0, x, self.height)
            canvas.restoreState()
            self.paragraph.drawOn(canvas, self.bar_width + self.gap, 0)

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

    collection_label = _window_label(
        project.get("collection_start"),
        project.get("collection_end"),
        empty="Não delimitado - busca temática",
    )

    if project.get("project_type") == "INSTITUTIONAL_PRODUCT":
        metadata = [
            [
                Paragraph("<b>Instituição</b><br/>" + escape(_text(project.get("institution"))), small),
                Paragraph("<b>Produto analisado</b><br/>" + escape(_text(project.get("topic"))), small),
            ],
            [
                Paragraph("<b>Período de monitoramento</b><br/>" + escape(collection_label), small),
                Paragraph(
                    f"<b>Corpus validado</b><br/>{metrics.get('valid_items', 0)} itens em {metrics.get('unique_vehicles', 0)} veículos",
                    small,
                ),
            ],
        ]
        if project.get("launch_date"):
            metadata.append(
                [
                    Paragraph("<b>Lançamento confirmado</b><br/>" + escape(_text(project.get("launch_date"))), small),
                    Paragraph("<b>Escopo</b><br/>Repercussão midiática do produto", small),
                ]
            )
    else:
        event_label = _window_label(
            project.get("event_start"),
            project.get("event_end"),
            empty="Não delimitado",
        )
        metadata = [
            [
                Paragraph("<b>Instituição</b><br/>" + escape(_text(project.get("institution"))), small),
                Paragraph("<b>Janela dos fatos</b><br/>" + escape(event_label), small),
            ],
            [
                Paragraph("<b>Janela de repercussão</b><br/>" + escape(collection_label), small),
                Paragraph(
                    f"<b>Corpus validado</b><br/>{metrics.get('valid_items', 0)} itens em {metrics.get('unique_vehicles', 0)} veículos",
                    small,
                ),
            ],
        ]
        metadata.append(
            [
                Paragraph("<b>Tipo de pauta</b><br/>" + escape(_text(project.get("project_type_label"))), small),
                Paragraph("<b>Escopo</b><br/>Repercussão midiática do tema", small),
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

    def add_section(name: str, content: object) -> bool:
        """Adiciona seção sem deixar o título órfão e sem prender textos longos."""
        if not _has_content(content):
            return False
        # O heading usa keepWithNext=True, então o título acompanha o início
        # do corpo. O parágrafo fica livre para continuar na página seguinte.
        story.append(Paragraph(name, heading))
        story.append(
            Paragraph(
                escape(_text(content)).replace("\n", "<br/>"),
                body,
            )
        )
        return True

    part_number = 0

    def add_part(name: str) -> None:
        """Numera somente partes que efetivamente serão renderizadas."""
        nonlocal part_number
        part_number += 1
        story.append(Paragraph(f"PARTE {part_number} - {name}", heading))

    cloud_words = list(word_cloud.get("words") or [])
    cross_validation_preview = metrics.get("youtube_cross_validation") or {}
    cross_validation_has_content = any(
        int(cross_validation_preview.get(key, 0) or 0) > 0
        for key in ("confirmed", "partial", "insufficient", "conflicts")
    ) or int(metrics.get("youtube_conflicts_excluded", 0) or 0) > 0
    panorama_has_content = any(
        (
            bool(cloud_words),
            _has_content(report.get("executive_summary")),
            _has_content(report.get("opening")),
            _has_content(report.get("panorama")),
            bool(corpus),
            bool(metrics.get("top_youtube_channels") or []),
            bool(metrics.get("top_reach_contents") or []),
            cross_validation_has_content,
            int(social_repercussion.get("comments") or 0) > 0,
            bool(public_opinion_surveys),
        )
    )
    if panorama_has_content:
        add_part("PANORAMA DA REPERCUSSÃO")
        if cloud_words:
            cloud_visual = _pdf_word_cloud_flowable(
                word_cloud,
                document.width,
            )
            merged_variants = int(word_cloud.get("merged_variants") or 0)
            cloud_note = (
                f"{word_cloud.get('documents', 0)} notícia(s) validada(s) de portais; "
                "stopwords e nomes de sites removidos"
            )
            if merged_variants:
                cloud_note += (
                    f"; {merged_variants} variante(s) linguística(s) consolidada(s)"
                )
            cloud_note += "."

            story.append(
                KeepTogether(
                    [
                        cloud_visual,
                        Spacer(1, 4),
                        Paragraph(cloud_note, cloud_note_style),
                    ]
                )
            )
        else:
            story.append(
                KeepTogether(
                    [
                        Paragraph(
                            "Nenhuma palavra relevante disponível no corpus jornalístico validado.",
                            small,
                        ),
                    ]
                )
            )


        executive_summary = report.get("executive_summary")
        if _has_content(executive_summary):
            story.append(Paragraph("Resumo Executivo", heading))
            story.append(
                LeftAccentParagraph(
                    escape(_text(executive_summary)).replace("\n", "<br/>"),
                    body,
                )
            )

        if corpus:
            story.append(Paragraph("Itens relacionados encontrados", heading))
            duplicate_total = sum(int(item.get("duplicate_count") or 0) for item in corpus)
            duplicate_note = (
                f" {duplicate_total} entrada(s) repetida(s) foram consolidadas para evitar dupla contagem."
                if duplicate_total
                else ""
            )
            social_monitoring_note = (
                f" Separadamente, {len(social_post_inventory)} post(s) social(is) foram monitorado(s) "
                "como base da camada de comentários; eles não são somados ao corpus jornalístico validado."
                if social_post_inventory
                else ""
            )
            story.append(
                Paragraph(
                    f"{len(corpus)} item(ns) único(s) validado(s) como materialmente relacionados ao tema: "
                    f"{len(social_items)} em mídias sociais no corpus validado, {len(youtube_items)} no YouTube e "
                    f"{len(portal_items)} em portais de notícias."
                    f"{duplicate_note}{social_monitoring_note} O detalhamento auditável está nos anexos.",
                    small,
                )
            )

        add_public_opinion_section(
            story,
            report=report,
            public_opinion=public_opinion,
            heading=heading,
            body=body,
            small=small,
        )
        add_social_section(
            story,
            social_repercussion=social_repercussion,
            heading=heading,
            body=body,
            small=small,
        )

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
            f"<b>{metrics.get('isp_mentioned_items', 0)} itens ({metrics.get('isp_mention_percent', 0)}%)</b> mencionam a instituição na amostra.",
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

        portal_checks = [
            item for item in metrics.get("portal_checks", [])
            if item.get("result") == "com cobertura auditável"
        ]
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

        priority_channels = [
            item for item in metrics.get("youtube_priority_channel_checks", [])
            if item.get("result") == "com cobertura auditável"
        ]
        if priority_channels:
            story.append(
                KeepTogether(
                    [
                        Paragraph("CHECAGEM DE CANAIS PRIORITÁRIOS NO YOUTUBE", heading),
                        _table(
                            [["Canal", "Resultado", "Vídeos", "Visualizações", "Link"]]
                            + [
                                [
                                    item.get("channel", "N/D"),
                                    item.get("result", "N/D"),
                                    item.get("videos", 0),
                                    _view_count_label(item.get("views")),
                                    _pdf_link(item.get("lead_url")),
                                ]
                                for item in priority_channels
                            ],
                            [4.2 * cm, 5.0 * cm, 1.8 * cm, 2.6 * cm, 3.0 * cm],
                            small,
                            header_color=colors.HexColor("#f0f0f0"),
                            header_text=colors.black,
                        ),
                    ]
                )
            )

        cross_validation = metrics.get("youtube_cross_validation", {})
        compared = sum(int(cross_validation.get(key, 0) or 0) for key in ("confirmed", "partial", "insufficient", "conflicts"))
        if compared or metrics.get("youtube_conflicts_excluded", 0):
            story.append(Paragraph("VALIDAÇÃO CRUZADA DE VÍDEOS", heading))
            story.append(
                Paragraph(
                    "<b>{confirmed}</b> confirmado(s), <b>{partial}</b> parcialmente confirmado(s), "
                    "<b>{insufficient}</b> com evidência insuficiente e <b>{conflicts}</b> conflito(s). "
                    "Itens com conflito material foram excluídos das tabelas de cobertura e ranking.".format(
                        confirmed=cross_validation.get("confirmed", 0),
                        partial=cross_validation.get("partial", 0),
                        insufficient=cross_validation.get("insufficient", 0),
                        conflicts=cross_validation.get("conflicts", 0),
                    ),
                    small,
                )
            )

        top_channels = metrics.get("top_youtube_channels", [])
        if top_channels:
            story.append(
                KeepTogether(
                    [
                        Paragraph("CANAIS NO YOUTUBE", heading),
                        _table(
                            [["#", "Canal", "Vídeos validados", "Visualizações", "Link"]]
                            + [
                                [
                                    index + 1,
                                    item.get("channel", "N/D"),
                                    item.get("videos", 0),
                                    _view_count_label(item.get("views")),
                                    _pdf_link(item.get("lead_url")),
                                ]
                                for index, item in enumerate(top_channels)
                            ],
                            [0.9 * cm, 5.9 * cm, 2.8 * cm, 3.0 * cm, 4.0 * cm],
                            small,
                            nowrap_columns={0},
                        ),
                    ]
                )
            )

        top_reach_contents = metrics.get("top_reach_contents", [])
        if top_reach_contents:
            story.append(Paragraph("CONTEÚDOS POR ALCANCE DISPONÍVEL", heading))
            story.append(
                Paragraph(
                    escape(_text(metrics.get("top_reach_methodology", ""))),
                    small,
                )
            )
            story.append(
                _table(
                    [["#", "Plataforma", "Fonte", "Título", "Alcance", "Link"]]
                    + [
                        [
                            index + 1,
                            item.get("platform", "N/D"),
                            item.get("source", "N/D"),
                            item.get("title", "N/D"),
                            _view_count_label(item.get("reach")),
                            _pdf_link(item.get("url")),
                        ]
                        for index, item in enumerate(top_reach_contents)
                    ],
                    [0.9 * cm, 1.6 * cm, 2.8 * cm, 6.1 * cm, 2.0 * cm, 3.2 * cm],
                    small,
                    nowrap_columns={0},
                )
            )

    if fact_layer_enabled and operations:
        add_part("OPERAÇÕES POLICIAIS IDENTIFICADAS")
        provisional_operations = any(
            operation.get("inventory_status") == "PROVISIONAL_MEDIA_MENTION"
            or operation.get("resolution_status") == "PROVISIONAL_MEDIA_MENTION"
            for operation in operations
        )
        story.append(Paragraph(
            "Operações citadas na amostra (inventário provisório)"
            if provisional_operations
            else "Inventário de operações identificadas",
            heading,
        ))
        story.append(Paragraph(
            (
                "A camada factual estruturada ainda não individualizou as operações; esta tabela é um fallback auditável "
                "construído apenas a partir de itens validados que citam operações."
                if provisional_operations
                else "Cada linha representa uma operação/evento individualizado na camada factual. Links oficiais sustentam "
                "a identificação factual e links de mídia apontam a repercussão associada localizada."
            ),
            small,
        ))
        op_rows = [["Mês", "Data", "Operação", "Local", "Força(s)", "Matérias", "Links"]]
        for operation in operations:
            event_date = str(operation.get("event_date") or "")
            month = event_date[:7] if len(event_date) >= 7 else "N/D"
            location_values = [*(operation.get("neighborhoods") or []), operation.get("city"), operation.get("state")]
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
        story.append(_table(
            op_rows,
            [1.25 * cm, 1.55 * cm, 3.2 * cm, 3.0 * cm, 2.5 * cm, 1.2 * cm, 4.0 * cm],
            small,
        ))

    thematic_axes = [
        item for item in (report.get("thematic_axes") or [])
        if _has_meaningful_fields(item, ("axis", "anchor_data", "coverage"))
    ]
    risk_assessment = [
        item for item in (report.get("risk_assessment") or [])
        if _has_meaningful_fields(item, ("dimension", "assessment", "evidence"))
    ]
    part3_has_content = any(
        (
            _has_content(report.get("dominant_framing")),
            bool(thematic_axes),
            _has_content(report.get("highest_yield")),
            _has_content(report.get("institutional_narrative")),
            bool(risk_assessment),
        )
    )
    if part3_has_content:
        add_part("ANÁLISE DA COBERTURA")
        add_section("Enquadramento Dominante", report.get("dominant_framing"))

        if thematic_axes:
            story.append(Paragraph("Um Estudo, Muitas Pautas", heading))
            story.append(
                _table(
                    [["Eixo temático", "Dado-âncora", "Cobertura"]]
                    + [
                        [
                            x.get("axis") or "N/D",
                            x.get("anchor_data") or "N/D",
                            x.get("coverage") or "N/D",
                        ]
                        for x in thematic_axes
                    ],
                    [4.1 * cm, 5.7 * cm, 6.8 * cm],
                    small,
                )
            )

        add_section(
            "Recorte de Maior Rendimento Jornalístico",
            report.get("highest_yield"),
        )
        add_section(
            "Camada Institucional e Disputa de Narrativa",
            report.get("institutional_narrative"),
        )

        if risk_assessment:
            story.append(Paragraph("Avaliação: Alcance, Profundidade e Riscos", heading))
            story.append(
                _table(
                    [["Dimensão", "Avaliação", "Evidência"]]
                    + [
                        [
                            x.get("dimension") or "N/D",
                            x.get("assessment") or "N/D",
                            x.get("evidence") or "N/D",
                        ]
                        for x in risk_assessment
                    ],
                    [3.6 * cm, 4.7 * cm, 8.3 * cm],
                    small,
                )
            )

    if fact_layer_enabled and facts:
        add_part("VERIFICAÇÃO FACTUAL")
        story.append(Paragraph("Camada de Fatos Verificados", heading))
        if _has_content(report.get("fact_layer_intro")):
            story.append(Paragraph(escape(_text(report.get("fact_layer_intro"))), body))

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
        story.append(
            _table(
                rows,
                [2.3 * cm, 2.5 * cm, 1.6 * cm, 2.8 * cm, 2.7 * cm, 2.7 * cm, 2.0 * cm],
                small,
            )
        )

    if academic_papers:
        add_part("CONTEXTUALIZAÇÃO CIENTÍFICA")
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
            published = _text(paper.get("published_at"))
            rows.append(
                [
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
                    _pdf_link(paper.get("url")),
                ]
            )
        story.append(
            _table(
                rows,
                [1.0 * cm, 3.2 * cm, 4.1 * cm, 5.4 * cm, 2.9 * cm],
                small,
            )
        )


    synthesis = report.get("synthesis")
    recommendations = [item for item in (report.get("recommendations") or []) if _has_content(item)]
    press_kit = [
        item for item in (report.get("press_kit") or [])
        if _has_meaningful_fields(item, ("product", "purpose"))
    ]
    if _has_content(synthesis) or recommendations or press_kit:
        add_part("SÍNTESE E ENCAMINHAMENTOS")
        add_section("Síntese", synthesis)

        if recommendations or press_kit:
            story.append(Paragraph("Recomendações e Kit de Imprensa", heading))
            if recommendations:
                story.extend(
                    [Paragraph("• " + escape(_text(item)), body) for item in recommendations if _has_content(item)]
                )
            if press_kit:
                story.append(
                    _table(
                        [["Produto", "Finalidade"]]
                        + [
                            [x.get("product") or "N/D", x.get("purpose") or "N/D"]
                            for x in press_kit
                        ],
                        [6.5 * cm, 10.1 * cm],
                        small,
                    )
                )

    def corpus_table(rows):
        if not rows:
            return Paragraph("Nenhum item validado nesta categoria para a janela observada.", small)
        return _table(
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
                    _pdf_link(item.get("url")),
                ]
                for index, item in enumerate(rows)
            ],
            [0.9 * cm, 2.3 * cm, 3.0 * cm, 9.2 * cm, 2.2 * cm],
            small,
            nowrap_columns={0, 1},
        )

    # Anexos vazios não entram no PDF. A numeração é atribuída apenas às
    # seções efetivamente renderizadas, evitando títulos sem conteúdo e letras
    # puladas quando uma categoria não possui itens.
    annex_sections: list[tuple[str, object, str]] = []
    methodological_note = report.get("methodological_note")
    if _has_content(methodological_note):
        annex_sections.append(("Nota Metodológica", methodological_note, "methodology"))
    if fact_layer_enabled and fact_evidence:
        annex_sections.append(("Evidências Factuais", fact_evidence, "facts"))
    if social_post_inventory:
        annex_sections.append(
            ("Mídias Sociais Monitoradas", social_post_inventory, "social_posts")
        )
    elif social_items:
        # Fallback para snapshots antigos que tinham item social VALID, mas não
        # possuíam ainda a camada SocialPost.
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
                    escape(_text(content)).replace("\n", "<br/>"),
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
                _table(
                    [["#", "Plataforma", "Data", "Post / referência", "Comentários", "Descoberta", "Link"]]
                    + [
                        [
                            index + 1,
                            item.get("platform") or "N/D",
                            item.get("published_at") or "N/D",
                            item.get("title") or "Post social",
                            int(item.get("comments_collected") or 0),
                            item.get("discovery_source") or "monitoramento social",
                            _pdf_link(item.get("url")),
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
                _table(
                    [["Pessoa", "Campo", "Valor", "Fonte", "Evidência", "Link"]]
                    + [
                        [
                            item.get("subject_name") or "N/D",
                            item.get("field") or "N/D",
                            item.get("value") or "N/D",
                            item.get("source") or item.get("source_type") or "N/D",
                            item.get("evidence") or "N/D",
                            _pdf_link(item.get("url")),
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
        story.append(corpus_table(rows))

    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buffer.getvalue()



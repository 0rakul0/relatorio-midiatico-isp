from io import BytesIO
from xml.sax.saxutils import escape

from app.reports.pdf.corpus import (
    deduplicated_corpus as _deduplicated_corpus,
    split_by_origin as _split_by_origin,
)
from app.reports.pdf.helpers import (
    pdf_link as _pdf_link,
    view_count_label as _view_count_label,
    window_label as _window_label,
)
from app.reports.pdf.word_cloud import pdf_word_cloud_flowable as _pdf_word_cloud_flowable
from app.reports.pdf.table import table as _table
from app.reports.pdf.public_opinion import add_public_opinion_section
from app.reports.pdf.social import add_social_section
from app.reports.pdf.operations import add_operations_section
from app.reports.pdf.facts import add_fact_section
from app.reports.pdf.academic import add_academic_section
from app.reports.pdf.annexes import add_audit_annexes
from app.reports.pdf.history import add_historical_comparison_section
from app.reports.pdf.layout import (
    LeftAccentParagraph,
    create_document,
    page_number,
    pdf_styles,
)
from app.utils.rendering import (
    has_content as _has_content,
    has_meaningful_fields as _has_meaningful_fields,
    text as _text,
)


def _build_cover_headlines_pdf(data: dict) -> bytes:
    """Evidence-only institutional front-page report, with historical indicators."""
    from collections import Counter
    from datetime import date
    from urllib.parse import urlparse
    from reportlab.lib import colors
    from reportlab.graphics.shapes import Drawing, Rect, String, Line
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, KeepTogether

    project = data["project"]
    styles = pdf_styles()
    raw = data.get("institutional_findings") or data.get("cover_findings") or []
    confirmed = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("confirmed") is not True:
            continue
        location = str(item.get("location") or item.get("editorial_location") or "").lower()
        if location not in {"front_page", "capa"}:
            continue
        evidence = [
            ev for ev in (item.get("evidence") or [])
            if isinstance(ev, dict) and ev.get("document_page") == 1
            and str(ev.get("url") or "").startswith(("https://", "http://"))
        ]
        if not evidence:
            continue
        day = str(item.get("publication_date") or "")[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        classification = str(item.get("classification") or "").lower()
        if classification not in {"explicit", "product", "indirect"}:
            continue
        if classification == "indirect" and not any(
            str(ev.get("source_type") or "").lower() in
            {"official_document", "official_data", "documentary_crosscheck"}
            for ev in evidence
        ):
            continue
        key = (str(item.get("publication") or "").casefold(), day,
               str(item.get("title") or "").casefold(), evidence[0]["url"])
        if key in seen:
            continue
        seen.add(key)
        confirmed.append({**item, "_cover_evidence": evidence[0],
                          "_classification": classification})
    confirmed.sort(key=lambda row: (str(row["publication_date"]), str(row.get("publication") or "")))
    counts = Counter(row["_classification"] for row in confirmed)
    papers = Counter(str(row.get("publication") or "Não identificado") for row in confirmed)
    products = Counter(str(row.get("product")) for row in confirmed if row.get("product"))
    monthly = {}
    for row in confirmed:
        month = str(row["publication_date"])[:7]
        bucket = monthly.setdefault(month, {"explicit": 0, "product": 0, "indirect": 0})
        bucket[row["_classification"]] += 1

    def p(value, style="body"):
        return Paragraph(escape(str(value)), styles[style])

    story = [
        p("INSTITUTO DE SEGURANÇA PÚBLICA", "small"),
        p("Presença do ISP em manchetes de capa", "title"),
        p("Monitoramento histórico de citações, produtos e uso de dados", "subtitle"),
        p(_window_label(project.get("collection_start"), project.get("collection_end"),
                        empty="Período não informado"), "small"),
        Spacer(1, 18),
        p("Resumo executivo", "heading"),
    ]
    cards = [
        ("Capas verificadas", len(confirmed)),
        ("Jornais", len(papers)),
        ("Menções explícitas", counts["explicit"]),
        ("Produtos ISP", counts["product"]),
        ("Usos indiretos comprovados", counts["indirect"]),
    ]
    cells = [Paragraph(
        f'<font color="#183b65" size="17"><b>{value}</b></font><br/>'
        f'<font color="#455569" size="8">{escape(label)}</font>', styles["body"]
    ) for label, value in cards]
    summary = Table([cells[:3], cells[3:] + [""]], colWidths=[170, 170, 170])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5fa")),
        ("TOPPADDING", (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.extend([summary, Spacer(1, 16), p("Evolução histórica da presença em capas", "heading")])
    if monthly:
        months = sorted(monthly)
        width, height = 510, 220
        chart = Drawing(width, height)
        left, bottom, plot_h, bar_w = 43, 43, 140, min(34, 370 / max(len(months), 1) * .65)
        maximum = max(sum(monthly[m].values()) for m in months) or 1
        colors_by_kind = {"explicit": "#305a9a", "product": "#b68b45", "indirect": "#408447"}
        chart.add(Line(left, bottom, 492, bottom, strokeColor=colors.HexColor("#a9b5c5")))
        for i, month in enumerate(months):
            x = left + (i + .5) * (445 / len(months))
            y = bottom
            for kind in ("explicit", "product", "indirect"):
                amount = monthly[month][kind]
                h = amount / maximum * plot_h
                if h:
                    chart.add(Rect(x - bar_w / 2, y, bar_w, h,
                                   fillColor=colors.HexColor(colors_by_kind[kind]),
                                   strokeColor=None))
                y += h
            if len(months) <= 18 or i % max(1, len(months)//12) == 0:
                chart.add(String(x, bottom - 13, month[2:], fontSize=7, textAnchor="middle"))
        chart.add(String(left, bottom + plot_h + 9, f"Máximo mensal: {maximum}", fontSize=8))
        for i, (kind, label) in enumerate([
            ("explicit", "Diretas"), ("product", "Produtos"), ("indirect", "Indiretas comprovadas")
        ]):
            x = 52 + i * 145
            chart.add(Rect(x, 9, 9, 9, fillColor=colors.HexColor(colors_by_kind[kind]), strokeColor=None))
            chart.add(String(x + 14, 10, label, fontSize=8))
        story.append(chart)
        story.append(p("Categorias exclusivas por manchete; uma capa pode conter mais de uma manchete.", "small"))
    else:
        story.append(p(
            "Não há manchetes de capa documentalmente confirmadas no conjunto fornecido. "
            "Isso não significa ausência de cobertura: é necessário verificar os acervos.", "body"))
    story.extend([Spacer(1, 14), p("Ranking de jornais", "heading")])
    if papers:
        rows = [[p("Jornal", "small"), p("Manchetes", "small")]]
        rows += [[p(name), str(n)] for name, n in papers.most_common()]
        table = Table(rows, colWidths=[370, 140], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7eef7")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        story.append(table)
    else:
        story.append(p("Nenhum jornal com capa comprovada.", "body"))
    story.extend([Spacer(1, 14), p("Produtos institucionais", "heading")])
    story.append(p(", ".join(f"{name}: {n}" for name, n in products.most_common())
                   if products else "Nenhum produto identificado em capas verificadas."))
    story.extend([Spacer(1, 14), p("Manchetes e evidências documentais", "heading")])
    for item in confirmed:
        url = str(item["_cover_evidence"]["url"])
        safe_url = escape(url, {'"': '&quot;'})
        title = escape(str(item.get("title") or "Sem título"))
        label = f'{item["publication_date"]} | {item.get("publication") or "Jornal"} | {item["_classification"]}'
        story.append(KeepTogether([
            p(label, "small"),
            Paragraph(f'<link href="{safe_url}" color="#245c9b">{title}</link>', styles["body"]),
            Spacer(1, 9),
        ]))
    story.extend([
        Spacer(1, 14), p("Metodologia e limitações", "heading"),
        p("A contagem inclui somente manchetes verificadas na página 1, com URL "
          "documental, data e classificação validada. Menções indiretas exigem "
          "evidência documental oficial adicional. Resultados de busca, notícias "
          "digitais e publicações em redes sociais não entram nos indicadores."),
        p("A falta de evidências não demonstra ausência de manchetes. "
          "A cobertura dos acervos e das edições não foi medida automaticamente.", "small"),
    ])
    buffer = BytesIO()
    document = create_document(buffer)
    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buffer.getvalue()


def build_pdf(data: dict) -> bytes:
    """Cria o PDF a partir do relatório persistido, sem chamar LLM novamente."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

    report = data["report"]
    project = data["project"]
    if str(project.get("execution_profile") or "").upper() == "MANCHETES_CAPA" or (project.get("execution_options") or {}).get("cover_only"):
        return _build_cover_headlines_pdf(data)
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

    style_map = pdf_styles()
    title = style_map["title"]
    subtitle = style_map["subtitle"]
    heading = style_map["heading"]
    body = style_map["body"]
    small = style_map["small"]
    cloud_note_style = style_map["cloud_note"]

    buffer = BytesIO()

    document = create_document(buffer)

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

    institutional_options = project.get("execution_options") or {}
    if institutional_options.get("institutional_presence"):
        story.append(Paragraph("Monitoramento da presença institucional", heading))
        story.append(Paragraph(
            "Este recorte é complementar à repercussão midiática geral. "
            "Os registros abaixo são candidatos para auditoria de atribuição; "
            "não constituem comprovação automática de uso indireto de dados.", body))
        institution = _text(project.get("institution") or "")
        products = [
            _text(p).strip() for p in
            (institutional_options.get("institutional_products") or [])
            if _text(p).strip()
        ]
        story.append(Paragraph(
            "Instituição: " + escape(institution) +
            ". Produtos informados: " +
            escape(", ".join(products) if products else "nenhum especificado") +
            ". Período: " + escape(collection_label) + ".", small))
        import re
        terms = [institution] + products
        # Siglas curtas exigem fronteiras de palavra; não inferir uso indireto.
        if "instituto de segurança pública" in institution.casefold():
            terms.append("ISP")
        candidates = []
        for item in corpus:
            content = " ".join(_text(item.get(k) or "") for k in
                               ("title", "snippet", "content", "summary"))
            matches = [
                term for term in terms if term and
                re.search(r"(?<!\\w)" + re.escape(term) + r"(?!\\w)",
                          content, flags=re.IGNORECASE)
            ]
            if matches:
                candidates.append((item, matches))
        story.append(Paragraph(
            f"{len(candidates)} registro(s) com correspondência textual "
            "no corpus deduplicado. Não foram contadas automaticamente "
            "capas, páginas internas nem utilizações indiretas sem evidência.", body))
        for item, matches in candidates[:60]:
            story.append(Paragraph(
                escape(_text(item.get("title") or "Publicação sem título")),
                small))
            story.append(Paragraph(
                "Termos: " + escape(", ".join(matches)) +
                " | Fonte: " + escape(_text(item.get("url") or "não informada")),
                small))
        if len(candidates) > 60:
            story.append(Paragraph(
                f"Exibidos os primeiros 60 de {len(candidates)} registros; "
                "o restante permanece no corpus do projeto.", small))

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

        if not institutional_options.get("institutional_presence"):
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
            (
                f"<b>{metrics.get('collection_days')} dia(s)</b> na janela de observação;"
                if project.get("collection_start") and project.get("collection_end")
                and metrics.get("collection_days") is not None
                else "<b>Período não delimitado</b> - busca temática;"
            ),
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
        add_operations_section(
            story,
            operations=operations,
            heading=heading,
            small=small,
        )

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

    # Mantém a comparação editorial separada da evidência factual atual.
    # Sem histórico aprovado não acrescenta páginas nem títulos vazios.
    historical = data.get("historical_analysis") or {}
    if historical.get("status") == "AVAILABLE" and historical.get("prior_reports"):
        add_part("EVOLUÇÃO DAS LEITURAS MIDIÁTICAS")
        add_historical_comparison_section(
            story,
            historical,
            heading=heading,
            body=body,
            small=small,
        )

    if fact_layer_enabled and facts:
        add_part("VERIFICAÇÃO FACTUAL")
        add_fact_section(
            story,
            facts=facts,
            report=report,
            heading=heading,
            body=body,
            small=small,
        )

    if academic_papers:
        add_part("CONTEXTUALIZAÇÃO CIENTÍFICA")
        add_academic_section(
            story,
            academic_papers=academic_papers,
            heading=heading,
            small=small,
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

    add_audit_annexes(
        story,
        report=report,
        fact_layer_enabled=fact_layer_enabled,
        fact_evidence=fact_evidence,
        social_post_inventory=social_post_inventory,
        social_items=social_items,
        youtube_items=youtube_items,
        portal_items=portal_items,
        heading=heading,
        body=body,
        small=small,
    )

    document.build(story, onFirstPage=page_number, onLaterPages=page_number)
    return buffer.getvalue()



from io import BytesIO

from pypdf import PdfReader

from app.pdf_report import build_pdf


def _item(url, domain, origin, title="Titulo"):
    return {
        "title": title,
        "url": url,
        "domain": domain,
        "media_origin": origin,
        "theme": "Geral",
        "evidence": "ev",
        "relation_type": "THEMATIC_CONTEXT",
        "relation_evidence": "ev",
        "corpus_origin": "SEARCH",
        "isp_mentioned": False,
        "published_at": "2026-08-10",
        "published_year": "2026",
        "source": domain,
        "view_count": None,
    }


def _data(corpus):
    return {
        "report": {
            "title": "Relatorio de teste",
            "interpretive_title": "Sub",
            "subtitle": "janela",
            "executive_summary": "Resumo.",
            "opening": "Abertura.",
            "panorama": "Panorama.",
            "dominant_framing": "Enquadramento.",
            "thematic_axes": [],
            "highest_yield": "Rendimento.",
            "institutional_narrative": "Institucional.",
            "risk_assessment": [],
            "recommendations": [],
            "press_kit": [],
            "synthesis": "Sintese.",
            "methodological_note": "Nota.",
        },
        "project": {
            "institution": "ISP",
            "topic": "Tema",
            "collection_start": "2026-08-01",
            "collection_end": "2026-08-31",
            "project_type": "GENERAL_TOPIC",
        },
        "metrics": {
            "valid_items": len(corpus),
            "unique_vehicles": 3,
            "discarded_items": 0,
            "collection_days": 31,
            "isp_mentioned_items": 0,
            "isp_mention_percent": 0,
        },
        "corpus": corpus,
        "fact_events": [],
        "fact_evidence": [],
        "qa": {},
    }


def _text(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def test_body_has_no_related_table_and_annexes_follow_origin_order():
    corpus = [
        _item("https://g1.globo.com/a", "g1.globo.com", "PORTAL_NOTICIAS", "Portal"),
        _item("https://x.com/u/status/1", "x.com", "REDE_SOCIAL", "Post"),
        _item("https://www.youtube.com/watch?v=abc", "www.youtube.com", "YOUTUBE", "Video"),
    ]
    text = _text(build_pdf(_data(corpus)))

    # Corpo: resumo quantitativo, sem a tabela de 10 itens.
    assert "Itens relacionados encontrados" in text
    assert "Exibindo 10 de" not in text
    assert "dias sociais" in text

    pos_social = text.index("Mídias Sociais")
    pos_youtube = text.index("YouTube", pos_social)
    pos_portal = text.index("Portais de Not")
    assert pos_social < pos_youtube < pos_portal

    # Cada item aparece depois do seu anexo.
    assert text.index("x.com/u/status/1") > pos_social
    assert text.index("watch?v=abc") > pos_youtube
    assert text.index("g1.globo.com/a") > pos_portal


def test_empty_categories_render_notice_in_annex():
    corpus = [_item("https://g1.globo.com/a", "g1.globo.com", "PORTAL_NOTICIAS")]
    text = _text(build_pdf(_data(corpus)))

    assert "Mídias Sociais" in text
    assert "Nenhum item validado nesta categoria" in text

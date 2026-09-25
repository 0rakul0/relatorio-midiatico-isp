from io import BytesIO

from pypdf import PdfReader

from app.pdf_report import _has_content, build_pdf


def _base_payload() -> dict:
    return {
        "report": {
            "title": "Relatório de teste",
            "interpretive_title": "Teste de seções condicionais",
            "subtitle": "Somente seções com conteúdo devem aparecer",
            "executive_summary": "Resumo executivo de teste.",
            "opening": "Abertura de teste.",
            "panorama": "Panorama de teste.",
            "dominant_framing": "",
            "thematic_axes": [],
            "highest_yield": "",
            "institutional_narrative": "",
            "risk_assessment": [],
            "fact_layer_intro": "",
            "synthesis": "Síntese de teste.",
            "recommendations": [],
            "press_kit": [],
            "methodological_note": "Nota metodológica de teste.",
        },
        "project": {
            "institution": "Instituto de Segurança Pública",
            "topic": "tema de teste",
            "project_type": "GENERAL_TOPIC",
            "project_type_label": "Tema geral",
            "collection_start": None,
            "collection_end": None,
            "event_start": None,
            "event_end": None,
            "execution_flags": {"enable_fact_layer": True},
        },
        "metrics": {
            "valid_items": 0,
            "unique_vehicles": 0,
            "discarded_items": 0,
            "collection_days": 0,
            "isp_mentioned_items": 0,
            "isp_mention_percent": 0,
        },
        "corpus": [],
        "corpus_by_origin": {},
        "fact_events": [],
        "operation_events": [],
        "fact_evidence": [],
        "academic_papers": [],
        "social_repercussion": {},
        "word_cloud": {},
    }


def _pdf_text(payload: dict) -> str:
    pdf = build_pdf(payload)
    reader = PdfReader(BytesIO(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_has_content_rejects_empty_structures():
    assert _has_content(None) is False
    assert _has_content("") is False
    assert _has_content("   ") is False
    assert _has_content([]) is False
    assert _has_content({}) is False
    assert _has_content({"items": []}) is False
    assert _has_content("conteúdo") is True
    assert _has_content([{"title": "item"}]) is True


def test_pdf_omits_empty_parts_and_empty_annex_categories():
    text = _pdf_text(_base_payload())

    assert "PARTE 2 - OPERAÇÕES POLICIAIS IDENTIFICADAS" not in text
    assert "PARTE 3 - ANÁLISE DA COBERTURA" not in text
    assert "PARTE 4 - VERIFICAÇÃO FACTUAL" not in text
    assert "PARTE 5 - CONTEXTUALIZAÇÃO CIENTÍFICA" not in text
    assert "Anexo B - Evidências Factuais" not in text
    assert "Mídias Sociais" not in text
    assert "Anexo B - YouTube" not in text
    assert "Portais de Notícias" not in text

    assert "PARTE 6 - SÍNTESE E ENCAMINHAMENTOS" in text
    assert "Anexo A - Nota Metodológica" in text

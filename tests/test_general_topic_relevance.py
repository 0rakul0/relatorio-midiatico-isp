from types import SimpleNamespace
from unittest.mock import patch

from app.services.validation import _review_media_relevance_batch


def test_general_topic_accepts_thematic_only_classification():
    project = SimpleNamespace(
        topic="violência contra mulheres no Brasil",
        project_type="GENERAL_TOPIC",
        institution="Instituto de Segurança Pública",
        topic_profile={},
    )
    item = SimpleNamespace(
        id=1,
        title="Reportagem sobre violência doméstica contra mulheres no Brasil",
        snippet="Mulheres relatam violência doméstica.",
        content="",
        source_name="Agência Brasil",
        domain="agenciabrasil.ebc.com.br",
        url="https://agenciabrasil.ebc.com.br/exemplo",
    )
    decision = {
        "decisions": [{
            "media_item_id": 1,
            "related": True,
            "relation_type": "THEMATIC_ONLY",
            "anchor": "Reportagem diretamente sobre violência contra mulheres",
        }]
    }
    with patch("app.services.validation.llm_is_configured", return_value=True), \
         patch("app.services.validation.get_report_agent") as agent:
        agent.return_value.run.return_value = decision
        decisions, calls = _review_media_relevance_batch(project, [item])
    assert calls == 1
    assert decisions[1][0] is True
    assert decisions[1][1] == "THEMATIC_ONLY"


def test_institutional_product_does_not_accept_thematic_only_classification():
    project = SimpleNamespace(
        topic="Dossiê Mulher 2026",
        project_type="INSTITUTIONAL_PRODUCT",
        institution="Instituto de Segurança Pública",
        topic_profile={},
    )
    item = SimpleNamespace(
        id=2, title="Violência doméstica no Brasil", snippet="Matéria sem Dossiê",
        content="", source_name="Portal", domain="portal.com",
        url="https://portal.com/noticia",
    )
    with patch("app.services.validation.llm_is_configured", return_value=True), \
         patch("app.services.validation.get_report_agent") as agent:
        agent.return_value.run.return_value = {
            "decisions": [{
                "media_item_id": 2, "related": True,
                "relation_type": "THEMATIC_ONLY",
                "anchor": "Tema semelhante sem menção ao produto",
            }]
        }
        decisions, _ = _review_media_relevance_batch(project, [item])
    assert decisions[2][0] is False

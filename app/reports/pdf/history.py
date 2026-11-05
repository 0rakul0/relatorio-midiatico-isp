"""Secao auditavel de evolucao entre leituras editoriais previamente aprovadas."""
from __future__ import annotations

from xml.sax.saxutils import escape

from reportlab.lib.units import cm
from reportlab.platypus import Paragraph

from app.reports.pdf.table import table
from app.utils.rendering import text


def add_historical_comparison_section(
    story: list,
    historical: dict,
    *,
    heading,
    body,
    small,
) -> bool:
    if historical.get("status") != "AVAILABLE":
        return False
    prior = list(historical.get("prior_reports") or [])
    if not prior:
        return False
    baseline = prior[0]
    basis = historical.get("comparison_basis") or {}
    interpretation = historical.get("interpretation") or {}

    story.append(Paragraph("Referência e limites da comparação", heading))
    version_reference = (
        f"Projeto {baseline.get('project_id')}; "
        f"versão {baseline.get('version_no')} "
        f"(registro {baseline.get('report_version_id')}); "
        f"gerada em {baseline.get('created_at') or 'data não informada'}."
    )
    story.append(Paragraph(escape(version_reference), small))
    story.append(
        table(
            [["Indicador documental", "Amostras comparadas"],
             ["Documentos com URL na leitura anterior",
              basis.get("previous_corpus_items_with_url", 0)],
             ["Documentos com URL na leitura atual",
              basis.get("current_corpus_items_with_url", 0)],
             ["Documentos encontrados nas duas amostras",
              basis.get("shared_document_count", 0)],
             ["Documentos exclusivos da amostra atual",
              basis.get("new_to_current_sample_count", 0)],
             ["Documentos da amostra anterior não presentes na atual",
              basis.get("absent_from_current_sample_count", 0)]],
            [11.1 * cm, 5.5 * cm],
            small,
        )
    )
    story.append(Paragraph(
        escape(text(basis.get("interpretation") or "")),
        small,
    ))
    if len(prior) > 1:
        story.append(Paragraph(
            f"A memória contém {len(prior)} versões anteriores aprovadas "
            "sobre o mesmo tema/edição; a tabela usa a versão mais recente "
            "como referência quantitativa.",
            small,
        ))

    summary = str(interpretation.get("summary") or "").strip()
    if summary:
        story.append(Paragraph("Mudanças na leitura editorial", heading))
        story.append(Paragraph(escape(summary), body))
    for key, label in (
        ("continuities", "Aspectos que permanecem"),
        ("changes", "Diferenças observadas nas análises"),
        ("caveats", "Ressalvas de comparabilidade"),
    ):
        values = [
            str(item).strip()
            for item in (interpretation.get(key) or [])[:4]
            if str(item).strip()
        ]
        if not values:
            continue
        story.append(Paragraph(label, heading))
        for value in values:
            story.append(Paragraph("&#8226; " + escape(value), body))

    story.append(Paragraph(
        "Nota metodológica: as versões anteriores são interpretações "
        "históricas armazenadas com QA aprovado, não fontes independentes "
        "de confirmação factual. Diferenças entre amostras podem refletir "
        "termos de busca, veículos, disponibilidade de dados ou janelas "
        "distintas; documentos exclusivos da amostra atual não são "
        "necessariamente publicações recentes.",
        small,
    ))
    return True

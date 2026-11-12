"""Pesquisa institucional complementar: planejamento, evidencias e PDF auditavel.

Nao substitui a coleta geral nem altera o periodo do projeto.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.orm import Session

from app.api.deps import project_or_404
from app.auth import AuthUser, get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/institutional", tags=["institutional-presence"])

class InstitutionalCriteria(BaseModel):
    explicit_mentions: bool = True
    institutional_products: bool = True
    verified_indirect_data: bool = True
    front_pages: bool = True
    inside_pages: bool = True
    digital_articles: bool = True

class InstitutionalRequest(BaseModel):
    criteria: InstitutionalCriteria = Field(default_factory=InstitutionalCriteria)
    products: list[str] = Field(default_factory=list, max_length=30)

class Evidence(BaseModel):
    url: HttpUrl
    description: str = Field(min_length=12)
    source_type: str = "article"
    document_page: int | None = Field(default=None, ge=1)

class InstitutionalFinding(BaseModel):
    title: str = Field(min_length=3)
    publication: str
    publication_date: str
    location: str
    classification: str
    article_url: HttpUrl
    product: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    confirmed: bool = False

class InstitutionalReportInput(BaseModel):
    institution: str = "Instituto de Seguranca Publica"
    period_label: str
    findings: list[InstitutionalFinding] = Field(default_factory=list)

def evidence_status(item: InstitutionalFinding) -> str:
    if item.classification not in {"explicit", "product", "indirect"}:
        return "review"
    if not item.evidence:
        return "review"
    return "confirmed" if item.confirmed else "review"

def plan_for_project(project, criteria: InstitutionalCriteria, products: list[str]) -> dict:
    institution = (project.institution or "Instituto de Seguranca Publica").strip()
    queries = []
    if criteria.explicit_mentions:
        queries += [f'"{institution}" imprensa', '"ISP" "seguranca publica" jornal']
    if criteria.institutional_products:
        queries += [f'"{p}" imprensa dados' for p in products if p.strip()]
    if criteria.verified_indirect_data:
        queries += [f'"{institution}" dados estatisticas fonte reportagem']
    locations = [name for enabled, name in [
        (criteria.front_pages, "front_page"),
        (criteria.inside_pages, "inside_page"),
        (criteria.digital_articles, "digital_article"),
    ] if enabled]
    return {
        "project_id": project.id,
        "institution": institution,
        "collection_start": str(project.collection_start),
        "collection_end": str(project.collection_end),
        "criteria": criteria.model_dump(),
        "locations": locations,
        "queries": list(dict.fromkeys(queries)),
        "steps": [
            "reuse_existing_corpus",
            "search_news_with_existing_providers",
            "check_historical_archive_availability",
            "retrieve_original_publication_and_source",
            "classify_and_verify_evidence",
            "human_review_uncertain_and_indirect",
            "generate_institutional_pdf",
        ],
        "warning": "Plano complementar; nao significa que os acervos impressos foram coletados.",
    }

@router.post("/projects/{project_id}/plan")
def institutional_plan(project_id: int, payload: InstitutionalRequest,
                       db: Session = Depends(get_db),
                       user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    if not any(payload.criteria.model_dump().values()):
        raise HTTPException(422, "Selecione pelo menos um criterio.")
    return plan_for_project(project, payload.criteria, payload.products)

@router.post("/validate")
def validate_findings(items: list[InstitutionalFinding]):
    counts = Counter(evidence_status(item) for item in items)
    return {
        "total": len(items),
        "confirmed": counts["confirmed"],
        "needs_review": counts["review"],
        "items": [{"title": item.title, "status": evidence_status(item)}
                  for item in items],
    }

@router.post("/report.pdf")
def institutional_pdf(payload: InstitutionalReportInput):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=45, rightMargin=45)
    base = getSampleStyleSheet()
    body = ParagraphStyle("InstitutionalBody", parent=base["BodyText"],
                          alignment=TA_JUSTIFY, leading=14, spaceAfter=8)
    story = [
        Paragraph("Presenca institucional na imprensa", base["Title"]),
        Paragraph(escape(payload.institution), base["Heading2"]),
        Paragraph("Periodo: " + escape(payload.period_label), body),
        Spacer(1, 12),
        Paragraph("Resumo executivo", base["Heading2"]),
    ]
    confirmed = [x for x in payload.findings if evidence_status(x) == "confirmed"]
    review = [x for x in payload.findings if evidence_status(x) != "confirmed"]
    story.append(Paragraph(
        f"Publicacoes analisadas: {len(payload.findings)}. "
        f"Confirmadas: {len(confirmed)}. Pendentes de revisao: {len(review)}. "
        "Os totais representam somente o material fornecido e validado, "
        "nao o universo completo de publicacoes.", body))
    story.append(Paragraph("Aparicoes confirmadas", base["Heading2"]))
    for item in confirmed:
        story.append(Paragraph(escape(item.title), base["Heading3"]))
        story.append(Paragraph(
            escape(f"{item.publication} | {item.publication_date} | "
                   f"{item.location} | {item.classification}"), body))
        story.append(Paragraph(escape(str(item.article_url)), body))
        for evidence in item.evidence:
            story.append(Paragraph(
                escape(evidence.description + " - " + str(evidence.url)), body))
    story.append(Paragraph("Metodologia e limitacoes", base["Heading2"]))
    story.append(Paragraph(
        "Menções explicitas, produtos institucionais e usos indiretos "
        "sao categorias distintas. O uso indireto so e confirmado com "
        "evidencia documental. Materias sem comprovacao ficam pendentes. "
        "A cobertura de acervos e edicoes deve ser informada antes de "
        "extrapolar os resultados para um periodo historico.", body))
    if review:
        story.append(Paragraph("Anexo: registros pendentes", base["Heading2"]))
        for item in review:
            story.append(Paragraph(
                escape(item.title + " - " + str(item.article_url)), body))
    doc.build(story)
    return Response(buf.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition":
                             'attachment; filename="presenca-institucional.pdf"'})

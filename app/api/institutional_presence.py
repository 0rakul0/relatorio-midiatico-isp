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

# Initial discovery vocabulary, not a claim that this is a complete
# or current official product catalogue. Expand only from verified sources.
DEFAULT_ISP_PRODUCTS = (
    "Dossiê Mulher",
    "ISP Dados",
    "ISP Cidades",
    "Dossiê Criança e Adolescente",
    "Dossiê Pessoa Idosa",
    "Dossiê LGBT+",
    "Mapa do Crime",
)


def institutional_cover_queries(project) -> list[str]:
    """Search only for primary newspaper cover evidence, not general articles."""
    from datetime import date
    start = getattr(project, "collection_start", None)
    end = getattr(project, "collection_end", None)
    if not start or not end:
        return []
    start_year = int(str(start)[:4])
    end_year = int(str(end)[:4])
    if end_year < start_year or end_year - start_year > 20:
        return []
    queries = []
    for year in range(start_year, end_year + 1):
        for newspaper in ("O Globo", "Extra", "O Dia"):
            queries.append(
                f'"{newspaper}" "capa" "Instituto de Segurança Pública" {year}'
            )
            queries.append(f'"{newspaper}" "primeira página" "ISP" {year}')
    return queries


def institutional_search_queries(project) -> list[str]:
    """Focused newspaper searches; no video, social or opinion research."""
    options = project.execution_options or {}
    if not options.get("institutional_presence"):
        return []
    institution = (project.institution or "Instituto de Segurança Pública").strip()
    products = [str(x).strip() for x in
                (options.get("institutional_products") or DEFAULT_ISP_PRODUCTS)
                if str(x).strip()]
    # Broad queries also capture explicit mentions not tied to a known product.
    queries = [
        f'"{institution}" jornal reportagem',
        '"ISP-RJ" jornal dados segurança',
        '"Instituto de Segurança Pública" capa jornal',
    ]
    queries.extend(f'"{product}" jornal dados' for product in products[:20])
    return list(dict.fromkeys(queries))


def plan_for_project(project, criteria: InstitutionalCriteria, products: list[str]) -> dict:
    institution = (project.institution or "Instituto de Seguranca Publica").strip()
    queries = []
    if not products:
        products = list(DEFAULT_ISP_PRODUCTS)
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

def corpus_candidates(db: Session, project, *, limit: int = 500) -> dict:
    """Inspect reusable project corpus without inventing archive/page evidence."""
    import re
    from sqlalchemy import select
    from app.models import CorpusDocument, ProjectCorpusLink

    options = project.execution_options or {}
    products = [str(p).strip() for p in options.get("institutional_products", [])
                if str(p).strip()]
    institution = (project.institution or "").strip()
    terms = list(dict.fromkeys([institution] + products))
    if "instituto de segurança pública" in institution.casefold():
        terms.append("ISP")
    if not terms:
        return {"total_scanned": 0, "candidates": [], "coverage": "NO_TERMS"}

    stmt = (select(CorpusDocument)
            .join(ProjectCorpusLink, ProjectCorpusLink.document_id == CorpusDocument.id)
            .where(ProjectCorpusLink.project_id == project.id)
            .order_by(CorpusDocument.published_at.desc())
            .limit(min(limit, 2000)))
    documents = db.scalars(stmt).all()
    candidates = []
    for doc in documents:
        if project.has_custom_date_window:
            if doc.published_at is None:
                continue
            if not (project.collection_start <= doc.published_at <= project.collection_end):
                continue
        content = " ".join(str(v or "") for v in
                           (doc.title, doc.snippet, doc.content))
        matched = [term for term in terms if re.search(
            r"(?<!\\w)" + re.escape(term) + r"(?!\\w)", content, re.IGNORECASE)]
        if not matched:
            continue
        candidates.append({
            "document_id": doc.id,
            "title": doc.title,
            "url": doc.url,
            "published_at": doc.published_at.isoformat() if doc.published_at else None,
            "source": doc.source_name or doc.domain,
            "matched_terms": matched,
            "classification": "candidate_text_match",
            "verification_status": "REVIEW_REQUIRED",
            "editorial_location": "UNKNOWN",
        })
    return {
        "total_scanned": len(documents),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "coverage": "PROJECT_LINKED_CORPUS_ONLY",
        "limitations": [
            "No print archive completeness is asserted.",
            "A textual match is not documentary proof of indirect use.",
            "Front page and inside page require independent page evidence.",
        ],
    }


@router.get("/projects/{project_id}/candidates")
def project_candidates(project_id: int, limit: int = 500,
                       db: Session = Depends(get_db),
                       user: AuthUser = Depends(get_current_user)):
    project = project_or_404(db, user, project_id)
    if not (project.execution_options or {}).get("institutional_presence"):
        raise HTTPException(409, "Investigacao institucional nao ativada neste projeto.")
    return corpus_candidates(db, project, limit=limit)


class ArchivePage(BaseModel):
    publication: str = Field(min_length=2)
    edition_date: str = Field(min_length=10, max_length=10)
    page_number: int = Field(ge=1)
    source_url: HttpUrl
    text: str = Field(min_length=30, max_length=300000)
    edition_id: str | None = None


class ArchiveBatch(BaseModel):
    institution: str = "Instituto de Segurança Pública"
    products: list[str] = Field(default_factory=list, max_length=30)
    pages: list[ArchivePage] = Field(min_length=1, max_length=200)


def extract_archive_matches(payload: ArchiveBatch) -> dict:
    """Deterministic extraction from supplied page text; no inferred provenance."""
    import re
    terms = list(dict.fromkeys(
        [payload.institution] + payload.products +
        (["ISP"] if "instituto de segurança pública" in payload.institution.casefold() else [])
    ))
    matches = []
    for page in payload.pages:
        for term in terms:
            if not term.strip():
                continue
            pattern = re.compile(r"(?<!\\w)" + re.escape(term) + r"(?!\\w)", re.I)
            for match in list(pattern.finditer(page.text))[:20]:
                excerpt = page.text[max(0, match.start()-110):min(len(page.text), match.end()+110)]
                matches.append({
                    "publication": page.publication,
                    "edition_date": page.edition_date,
                    "edition_id": page.edition_id,
                    "page_number": page.page_number,
                    "editorial_location": "front_page" if page.page_number == 1 else "inside_page",
                    "source_url": str(page.source_url),
                    "matched_term": term,
                    "excerpt": excerpt,
                    "status": "REVIEW_REQUIRED",
                    "note": "Page number and supplied text are not independently verified.",
                })
    return {
        "pages_processed": len(payload.pages),
        "textual_occurrences": len(matches),
        "matches": matches,
        "limitations": "No automated proof of indirect data usage; check archive authenticity and edition pagination.",
    }


@router.post("/archive/pages/analyze")
def analyze_archive_pages(payload: ArchiveBatch):
    return extract_archive_matches(payload)


def verify_indirect_citation(article_text: str, official_excerpt: str,
                             official_source_url: str) -> dict:
    """Conservative deterministic precheck, not an attribution verdict."""
    import re
    if not official_source_url.startswith(("https://", "http://")):
        return {"status": "REVIEW_REQUIRED", "reason": "Missing source URL"}
    normalize = lambda s: " ".join(re.findall(r"\\w+", s.casefold()))
    quote = normalize(official_excerpt)
    article = normalize(article_text)
    if len(quote) < 45 or quote not in article:
        return {"status": "REVIEW_REQUIRED", "reason": "Exact long excerpt not found"}
    return {
        "status": "EVIDENCE_MATCH_REVIEW_REQUIRED",
        "reason": "Long excerpt matches, but independent source attribution and publication chronology still require verification.",
        "source_url": official_source_url,
    }


class IndirectEvidenceCheck(BaseModel):
    article_text: str = Field(min_length=1, max_length=300000)
    official_excerpt: str = Field(min_length=1, max_length=10000)
    official_source_url: HttpUrl


@router.post("/indirect/check")
def check_indirect_evidence(payload: IndirectEvidenceCheck):
    return verify_indirect_citation(
        payload.article_text, payload.official_excerpt, str(payload.official_source_url)
    )


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

@router.post("/cover-report.pdf")
def institutional_cover_pdf(payload: InstitutionalReportInput):
    """Generate the dedicated cover report from audited structured findings."""
    from app.pdf_report import build_pdf
    findings = [item.model_dump(mode="json") for item in payload.findings]
    result = build_pdf({
        "report": {},
        "project": {
            "execution_profile": "MANCHETES_CAPA",
            "collection_start": None,
            "collection_end": None,
        },
        "institutional_findings": findings,
    })
    return Response(content=result, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="presenca-isp-capas.pdf"'})


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
    from collections import defaultdict
    by_year = Counter(x.publication_date[:4] for x in confirmed)
    by_location = Counter(x.location for x in confirmed)
    by_kind = Counter(x.classification for x in confirmed)
    by_publication = Counter(x.publication for x in confirmed)
    story.append(Paragraph("Indicadores editoriais", base["Heading2"]))
    for label, counts in [
        ("Por tipo de evidencia", by_kind),
        ("Por posicao editorial", by_location),
        ("Por veiculo", by_publication),
    ]:
        story.append(Paragraph(escape(label), base["Heading3"]))
        if counts:
            rows = [[Paragraph("Categoria", base["Normal"]), Paragraph("Ocorrencias", base["Normal"])]]
            rows += [[Paragraph(escape(str(k)), base["Normal"]), str(v)]
                     for k, v in counts.most_common(20)]
            table = Table(rows, colWidths=[350, 130], repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf1f6")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#8ba6b8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(table)
        else:
            story.append(Paragraph("Sem registros confirmados.", body))
    story.append(Paragraph("Serie historica de aparicoes confirmadas", base["Heading2"]))
    if by_year:
        from reportlab.graphics.shapes import Drawing
        from reportlab.graphics.charts.barcharts import VerticalBarChart
        years = sorted(by_year)
        chart = VerticalBarChart()
        chart.x = 45
        chart.y = 35
        chart.height = 125
        chart.width = 410
        chart.data = [[by_year[y] for y in years]]
        chart.categoryAxis.categoryNames = years
        chart.valueAxis.valueMin = 0
        chart.valueAxis.valueMax = max(by_year.values()) + 1
        chart.valueAxis.valueStep = max(1, (max(by_year.values()) + 3) // 4)
        drawing = Drawing(500, 180)
        drawing.add(chart)
        story.append(drawing)
    else:
        story.append(Paragraph("Sem datas confirmadas para gerar serie historica.", body))
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

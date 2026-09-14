from datetime import date, datetime
from urllib.parse import urlparse, urlunparse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.llm import structured_response
from app.models import Classification, GeneratedReport, MediaItem, OfficialFact, Project, SearchQuery
from app.pdf_report import build_pdf
from app.prompts import ANALYST_PROMPT, DOCUMENTALIST_PROMPT, QUERY_PLANNER_PROMPT, WRITER_PROMPT


PRIORITY_PORTALS = [
    ("G1/Globo", "g1.globo.com"),
    ("O Globo", "oglobo.globo.com"),
    ("Extra", "extra.globo.com"),
    ("CNN Brasil", "cnnbrasil.com.br"),
    ("UOL", "uol.com.br"),
    ("Agência Brasil", "agenciabrasil.ebc.com.br"),
]
PRIORITY_DOMAINS = [domain for _, domain in PRIORITY_PORTALS]


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def result_publication_date(value: object) -> date | None:
    """Converte a data publicada pela busca, quando a fonte a disponibiliza."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def discover_project_profile(db: Session, project: Project) -> dict:
    """Localiza fontes e extrai perfil documental antes da série histórica."""
    has_custom_window = project.status == "CUSTOM_DATES"
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY não configurada")
    from tavily import TavilyClient

    client = TavilyClient(api_key=key)
    searches = [
        f'"{project.topic}" "Instituto de Segurança Pública"',
        f'site:isp.rj.gov.br "{project.topic}"',
        f'"{project.topic}" lançamento',
    ]
    sources, seen = [], set()
    for query in searches:
        try:
            response = client.search(query=query, max_results=5, include_raw_content="text", search_depth="advanced", timeout=10)
        except Exception:
            # A descoberta é melhor-esforço: uma fonte indisponível não bloqueia o projeto.
            continue
        for result in response.get("results", []):
            url = result.get("url")
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": result.get("title", "Sem título"), "url": url,
                                "content": (result.get("raw_content") or result.get("content") or "")[:8000]})

    if not sources:
        project.status = "PROFILE_NEEDS_REVIEW"
        db.commit()
        return {"sources": 0, "facts": 0, "launch_date_confirmed": False}
    result = structured_response(
        instructions=DOCUMENTALIST_PROMPT + " Use somente as fontes recebidas. Identifique a instituição responsável e a data de lançamento apenas se houver evidência textual. A data deve ser ISO (AAAA-MM-DD) ou nula.",
        payload={"topic": project.topic, "sources": sources},
        schema_name="project_profile",
        schema={"type": "object", "additionalProperties": False, "properties": {
            "institution": {"type": ["string", "null"]}, "launch_date": {"type": ["string", "null"]},
            "official_facts": {"type": "array", "maxItems": 20, "items": {"type": "object", "additionalProperties": False, "properties": {
                "label": {"type": "string"}, "value": {"type": "string"}, "evidence": {"type": "string"}, "source_index": {"type": "integer", "minimum": 0}
            }, "required": ["label", "value", "evidence", "source_index"]}}
        }, "required": ["institution", "launch_date", "official_facts"]},
    )
    if result["institution"]:
        project.institution = result["institution"]
    launch_confirmed = False
    if result["launch_date"]:
        try:
            launch = date.fromisoformat(result["launch_date"])
            if launch <= date.today():
                project.launch_date = launch
                if not has_custom_window:
                    project.collection_start = launch
                launch_confirmed = True
        except ValueError:
            pass
    project.collection_end = date.today()
    project.status = "PROFILED" if launch_confirmed else "PROFILE_NEEDS_REVIEW"
    facts_added = 0
    for fact in result["official_facts"]:
        source_index = fact["source_index"]
        if source_index >= len(sources):
            continue
        source = sources[source_index]
        exists = db.scalar(select(OfficialFact.id).where(OfficialFact.project_id == project.id, OfficialFact.label == fact["label"], OfficialFact.evidence == fact["evidence"]))
        if not exists:
            db.add(OfficialFact(project_id=project.id, label=fact["label"][:250], value=fact["value"], source_reference=source["url"][:500], evidence=fact["evidence"], page=None))
            facts_added += 1
    db.commit()
    return {"sources": len(sources), "facts": facts_added, "institution": project.institution, "launch_date": str(project.launch_date), "launch_date_confirmed": launch_confirmed, "collection_end": str(project.collection_end), "status": project.status}


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    terms = [fact.label for fact in db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()]
    base = [
        (f'"{project.topic}"', "geral", "Localizar citações nominais ao produto", 1),
        (f'"{project.topic}" "{project.institution}"', "geral", "Localizar cobertura que identifica a instituição", 1),
        (f'site:youtube.com "{project.topic}"', "youtube", "Localizar vídeos e matérias publicadas no YouTube", 2),
    ]
    base += [(f'"{project.topic}" "{term}"', "tematica", f"Indicador oficial: {term}", 1) for term in terms[:12]]
    base += [(f'site:{domain} "{project.topic}"', "veiculo", f"Checagem nominal: {domain}", 2) for domain in PRIORITY_DOMAINS]
    existing = set(db.scalars(select(SearchQuery.query).where(SearchQuery.project_id == project.id)).all())
    created = []
    for query, kind, rationale, priority in base:
        if query not in existing:
            row = SearchQuery(project_id=project.id, query=query, kind=kind, rationale=rationale, priority=priority)
            db.add(row); created.append(row)
    db.commit()
    return created


def plan_queries_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    """Planeja consultas pela LLM e grava a mesma trilha de auditoria do planejador local."""
    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    result = structured_response(
        instructions=QUERY_PLANNER_PROMPT,
        payload={"project": {"topic": project.topic, "institution": project.institution}, "official_facts": [
            {"label": fact.label, "value": fact.value, "source_reference": fact.source_reference, "page": fact.page, "evidence": fact.evidence}
            for fact in facts
        ]},
        schema_name="search_plan",
        schema={"type": "object", "additionalProperties": False, "properties": {"queries": {"type": "array", "maxItems": 30, "items": {
            "type": "object", "additionalProperties": False, "properties": {
                "query": {"type": "string"}, "kind": {"type": "string"}, "rationale": {"type": "string"}, "priority": {"type": "integer", "minimum": 1, "maximum": 3}
            }, "required": ["query", "kind", "rationale", "priority"]
        }}}, "required": ["queries"]},
    )
    existing = set(db.scalars(select(SearchQuery.query).where(SearchQuery.project_id == project.id)).all())
    created = []
    for item in result["queries"]:
        query = item["query"].strip()
        if query and query not in existing:
            row = SearchQuery(project_id=project.id, **item)
            db.add(row); created.append(row); existing.add(query)
    youtube_query = f'site:youtube.com "{project.topic}"'
    if youtube_query not in existing:
        row = SearchQuery(project_id=project.id, query=youtube_query, kind="youtube", rationale="Localizar vídeos e matérias publicadas no YouTube", priority=2)
        db.add(row); created.append(row)
    db.commit()
    return created


def collect_tavily(db: Session, project_id: int) -> int:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY não configurada")
    from tavily import TavilyClient
    client = TavilyClient(api_key=key)
    project = db.get(Project, project_id)
    if not project:
        raise RuntimeError("Projeto não encontrado")
    added = 0
    known_canonicals = set(db.scalars(select(MediaItem.canonical_url).where(MediaItem.project_id == project_id)).all())
    queries = db.scalars(select(SearchQuery).where(SearchQuery.project_id == project_id, SearchQuery.executed_at.is_(None))).all()
    for query in queries:
        response = client.search(query=query.query, max_results=10, include_raw_content="text", start_date=str(project.collection_start), end_date=str(project.collection_end), topic="general" if query.kind == "youtube" else "news")
        for result in response.get("results", []):
            url = result["url"]; canonical = canonicalize(url)
            if canonical not in known_canonicals:
                db.add(MediaItem(project_id=project_id, query_id=query.id, title=result.get("title", "Sem título"), url=url,
                    canonical_url=canonical, domain=urlparse(url).netloc, published_at=result_publication_date(result.get("published_date")),
                    snippet=result.get("content"), content=result.get("raw_content"), search_source="youtube" if query.kind == "youtube" else "tavily"))
                known_canonicals.add(canonical); added += 1
        query.executed_at = datetime.utcnow()
    db.commit()
    return added


def validate_and_classify(db: Session, project: Project) -> dict:
    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    valid = discarded = 0
    topic_words = set(project.topic.lower().replace('"', '').split())
    for item in items:
        body = " ".join(filter(None, [item.title, item.snippet, item.content])).lower()
        related = len(topic_words.intersection(set(body.split()))) >= 1 or "instituto de segurança pública" in body or " isp " in f" {body} "
        if not related:
            item.status, item.discard_reason = "NOT_RELATED", "Sem evidência textual suficiente de relação com o tema ou ISP"
            discarded += 1; continue
        item.status, item.discard_reason = "VALID", None
        theme = next((fact.label for fact in db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all() if fact.label.lower() in body), "Geral")
        mention = "instituto de segurança pública" in body or " isp " in f" {body} "
        classification = db.scalar(select(Classification).where(Classification.media_item_id == item.id))
        if not classification:
            db.add(Classification(media_item_id=item.id, theme=theme, framing="A determinar por revisão analítica", isp_mentioned=mention,
                tone_toward_institution="NEUTRO", fidelity_status="PENDENTE", evidence=(item.snippet or item.title)[:1000], errors=[]))
        valid += 1
    db.commit()
    return {"valid": valid, "discarded": discarded}


def classify_with_llm(db: Session, project: Project) -> dict:
    """Classifica apenas itens previamente validados; não concede ferramentas à LLM."""
    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id, MediaItem.status == "VALID")).all()
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "theme": {"type": "string"}, "framing": {"type": "string"}, "isp_mentioned": {"type": "boolean"},
        "tone_toward_institution": {"type": "string", "enum": ["POSITIVO", "NEUTRO", "NEGATIVO", "INVERIFICÁVEL"]},
        "fidelity_status": {"type": "string", "enum": ["FIEL", "DIVERGENTE", "INVERIFICÁVEL"]},
        "evidence": {"type": "string"}, "errors": {"type": "array", "items": {"type": "string"}}
    }, "required": ["theme", "framing", "isp_mentioned", "tone_toward_institution", "fidelity_status", "evidence", "errors"]}
    official_facts = [{"label": fact.label, "value": fact.value, "evidence": fact.evidence} for fact in facts]
    updated = 0
    for item in items:
        result = structured_response(instructions=ANALYST_PROMPT, payload={"official_facts": official_facts, "media_item": {
            "id": item.id, "title": item.title, "url": item.url, "snippet": item.snippet, "content": (item.content or "")[:12000]
        }}, schema_name="media_classification", schema=schema)
        classification = db.scalar(select(Classification).where(Classification.media_item_id == item.id))
        if not classification:
            classification = Classification(media_item_id=item.id, **result)
            db.add(classification)
        else:
            for field, value in result.items(): setattr(classification, field, value)
        updated += 1
    db.commit()
    return {"updated": updated}


def draft_report_with_llm(db: Session, project: Project) -> dict:
    """Gera relatório estruturado a partir de métricas SQL e corpus validado, sem ferramentas."""
    data = metrics(db, project.id)
    items = db.execute(select(MediaItem, Classification).join(Classification).where(
        MediaItem.project_id == project.id, MediaItem.status == "VALID")).all()
    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    report_schema = {"type": "object", "additionalProperties": False, "properties": {
        "title": {"type": "string"}, "interpretive_title": {"type": "string"}, "subtitle": {"type": "string"},
        "executive_summary": {"type": "string"}, "opening": {"type": "string"}, "panorama": {"type": "string"},
        "dominant_framing": {"type": "string"}, "highest_yield": {"type": "string"}, "institutional_narrative": {"type": "string"},
        "synthesis": {"type": "string"}, "methodological_note": {"type": "string"},
        "thematic_axes": {"type": "array", "maxItems": 10, "items": {"type": "object", "additionalProperties": False, "properties": {
            "axis": {"type": "string"}, "anchor_data": {"type": "string"}, "coverage": {"type": "string"}
        }, "required": ["axis", "anchor_data", "coverage"]}},
        "risk_assessment": {"type": "array", "maxItems": 8, "items": {"type": "object", "additionalProperties": False, "properties": {
            "dimension": {"type": "string"}, "assessment": {"type": "string"}, "evidence": {"type": "string"}
        }, "required": ["dimension", "assessment", "evidence"]}},
        "recommendations": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "press_kit": {"type": "array", "maxItems": 10, "items": {"type": "object", "additionalProperties": False, "properties": {
            "product": {"type": "string"}, "purpose": {"type": "string"}
        }, "required": ["product", "purpose"]}}
    }, "required": ["title", "interpretive_title", "subtitle", "executive_summary", "opening", "panorama", "dominant_framing", "thematic_axes", "highest_yield", "institutional_narrative", "risk_assessment", "recommendations", "press_kit", "synthesis", "methodological_note"]}
    result = structured_response(
        instructions=WRITER_PROMPT + " Estruture exatamente como: Resumo Executivo; Abertura; I. Panorama da Repercussão; II. Enquadramento Dominante; III. Um Estudo, Muitas Pautas; IV. Recorte de Maior Rendimento Jornalístico; V. Camada Institucional e Disputa de Narrativa; VI. Avaliação: Alcance, Profundidade e Riscos; VII. Recomendações e Kit de Imprensa; VIII. Síntese; Anexo A - Nota Metodológica. Não afirme informação que não esteja nas métricas, fatos oficiais ou itens validados. O corpus auditável será anexado pelo sistema, portanto não invente URLs.",
        payload={"project": {"topic": project.topic, "institution": project.institution, "collection_start": str(project.collection_start), "collection_end": str(project.collection_end)},
                 "metrics": data, "official_facts": [{"label": fact.label, "value": fact.value, "evidence": fact.evidence, "source": fact.source_reference} for fact in facts],
                 "validated_items": [{"title": item.title, "url": item.url, "evidence": classification.evidence, "theme": classification.theme, "framing": classification.framing} for item, classification in items]},
        schema_name="structured_media_report",
        schema=report_schema,
    )
    corpus = corpus_for_project(db, project.id)
    payload = {"report": result, "metrics": data, "corpus": corpus, "project": {"id": project.id, "topic": project.topic, "institution": project.institution, "launch_date": str(project.launch_date), "collection_start": str(project.collection_start), "collection_end": str(project.collection_end)}}
    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if saved:
        saved.body = payload
    else:
        db.add(GeneratedReport(project_id=project.id, body=payload))
    db.commit()
    return payload


def export_report_pdf(db: Session, project: Project) -> bytes:
    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    payload = saved.body if saved else draft_report_with_llm(db, project)
    # O texto permanece o relatório aprovado; os indicadores são recalculados para
    # que a exportação reflita o corpus armazenado, inclusive em relatórios antigos.
    payload["metrics"] = metrics(db, project.id)
    payload["corpus"] = corpus_for_project(db, project.id)
    return build_pdf(payload)


def corpus_for_project(db: Session, project_id: int) -> list[dict]:
    """Retorna exclusivamente o corpus validado para análise e preserva sua rastreabilidade."""
    rows = db.execute(
        select(MediaItem, Classification)
        .outerjoin(Classification, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.desc(), MediaItem.id.desc())
    ).all()
    return [{
        "title": item.title,
        "url": item.url,
        "domain": item.domain,
        "theme": classification.theme if classification else None,
        "evidence": classification.evidence if classification else None,
        "published_year": str(item.published_at.year) if item.published_at else "Não informado",
        "source": "YouTube" if "youtube.com" in (item.domain or "").lower() else (item.domain or "Fonte aberta"),
    } for item, classification in rows]


def _hydrate_cached_report(db: Session, project: Project, generated: GeneratedReport) -> dict:
    payload = dict(generated.body)
    payload["project"] = {
        "id": project.id, "topic": project.topic, "institution": project.institution,
        "launch_date": str(project.launch_date), "collection_start": str(project.collection_start),
        "collection_end": str(project.collection_end),
    }
    payload["metrics"] = metrics(db, project.id)
    payload["corpus"] = corpus_for_project(db, project.id)
    payload["cached_at"] = generated.generated_at.isoformat() if generated.generated_at else None
    return payload


def cached_report_for_topic(db: Session, topic: str, collection_start: date | None = None, collection_end: date | None = None) -> dict | None:
    """Recupera a versão mais recente já concluída para o mesmo tema, sem usar APIs."""
    normalized_topic = topic.strip().casefold()
    statement = (
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(func.lower(Project.topic) == normalized_topic)
        .order_by(GeneratedReport.generated_at.desc())
    )
    if collection_start:
        statement = statement.where(Project.collection_start == collection_start)
    if collection_end:
        statement = statement.where(Project.collection_end == collection_end)
    row = db.execute(statement).first()
    return _hydrate_cached_report(db, *row) if row else None


def cached_report_for_project(db: Session, project_id: int) -> dict | None:
    row = db.execute(
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(Project.id == project_id)
    ).first()
    return _hydrate_cached_report(db, *row) if row else None


def run_full_methodology(db: Session, project: Project) -> dict:
    """Executa a metodologia completa a partir de um tema, preservando cada etapa auditável."""
    profile = discover_project_profile(db, project) if project.status == "DRAFT" else {
        "status": project.status, "institution": project.institution, "launch_date": str(project.launch_date),
        "collection_end": str(project.collection_end), "facts": 0, "sources": 0,
    }
    if project.status == "PROFILE_NEEDS_REVIEW":
        raise RuntimeError("Não foi possível confirmar o lançamento com evidência suficiente; revise o perfil antes da coleta.")
    planned = plan_queries_with_llm(db, project)
    collected = collect_tavily(db, project.id)
    validation = validate_and_classify(db, project)
    drafted = draft_report_with_llm(db, project)
    project.status = "REPORT_READY"
    db.commit()
    return {"project": {"id": project.id, "topic": project.topic, "institution": project.institution,
                         "launch_date": str(project.launch_date), "collection_start": str(project.collection_start),
                         "collection_end": str(project.collection_end)}, "profile": profile, "planned": len(planned),
            "collected": collected, "validation": validation, **drafted}


def metrics(db: Session, project_id: int) -> dict:
    total = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id)) or 0
    valid = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")) or 0
    vehicles = db.scalar(select(func.count(func.distinct(MediaItem.domain))).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")) or 0
    isp = db.scalar(select(func.count(Classification.id)).join(MediaItem).where(MediaItem.project_id == project_id, MediaItem.status == "VALID", Classification.isp_mentioned.is_(True))) or 0
    themes = db.execute(select(Classification.theme, func.count(Classification.id).label("items")).join(MediaItem).where(MediaItem.project_id == project_id, MediaItem.status == "VALID").group_by(Classification.theme).order_by(func.count(Classification.id).desc())).all()
    domains = [domain.lower() for domain in db.scalars(select(MediaItem.domain).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")).all() if domain]
    portal_checks = []
    for portal, domain in PRIORITY_PORTALS:
        found = sum(item == domain or item.endswith("." + domain) for item in domains)
        portal_checks.append({
            "portal": portal,
            "result": "com cobertura auditável" if found else "sem item validado na amostra",
            "evidence": f"{found} item(ns) validado(s) no domínio da amostra." if found else "Nenhum item validado nesse domínio no corpus coletado.",
        })
    project = db.get(Project, project_id)
    collection_days = ((project.collection_end - project.collection_start).days + 1) if project else 0
    return {"items_found": total, "valid_items": valid, "discarded_items": total - valid, "unique_vehicles": vehicles,
            "collection_days": collection_days, "isp_mentioned_items": isp,
            "isp_protagonism_percent": round((isp / valid * 100), 1) if valid else 0,
            "themes": [{"theme": x[0], "items": x[1]} for x in themes], "portal_checks": portal_checks}

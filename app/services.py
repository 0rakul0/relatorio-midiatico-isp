from datetime import datetime
from urllib.parse import urlparse, urlunparse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.models import Classification, MediaItem, OfficialFact, Project, SearchQuery


PRIORITY_DOMAINS = ["g1.globo.com", "oglobo.globo.com", "extra.globo.com", "cnnbrasil.com.br", "uol.com.br", "agenciabrasil.ebc.com.br"]


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    terms = [fact.label for fact in db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()]
    base = [
        (f'"{project.topic}"', "geral", "Localizar citações nominais ao produto", 1),
        (f'"{project.topic}" "{project.institution}"', "geral", "Localizar cobertura que identifica a instituição", 1),
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


def collect_tavily(db: Session, project_id: int) -> int:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY não configurada")
    from tavily import TavilyClient
    client = TavilyClient(api_key=key)
    added = 0
    queries = db.scalars(select(SearchQuery).where(SearchQuery.project_id == project_id, SearchQuery.executed_at.is_(None))).all()
    for query in queries:
        response = client.search(query=query.query, max_results=10, include_raw_content="text")
        for result in response.get("results", []):
            url = result["url"]; canonical = canonicalize(url)
            exists = db.scalar(select(MediaItem.id).where(MediaItem.project_id == project_id, MediaItem.canonical_url == canonical))
            if not exists:
                db.add(MediaItem(project_id=project_id, query_id=query.id, title=result.get("title", "Sem título"), url=url,
                    canonical_url=canonical, domain=urlparse(url).netloc, snippet=result.get("content"), content=result.get("raw_content"), search_source="tavily"))
                added += 1
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


def metrics(db: Session, project_id: int) -> dict:
    total = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id)) or 0
    valid = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")) or 0
    vehicles = db.scalar(select(func.count(func.distinct(MediaItem.domain))).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")) or 0
    isp = db.scalar(select(func.count(Classification.id)).join(MediaItem).where(MediaItem.project_id == project_id, MediaItem.status == "VALID", Classification.isp_mentioned.is_(True))) or 0
    themes = db.execute(select(Classification.theme, func.count(Classification.id).label("items")).join(MediaItem).where(MediaItem.project_id == project_id, MediaItem.status == "VALID").group_by(Classification.theme).order_by(func.count(Classification.id).desc())).all()
    return {"items_found": total, "valid_items": valid, "discarded_items": total - valid, "unique_vehicles": vehicles,
            "isp_protagonism_percent": round((isp / valid * 100), 1) if valid else 0, "themes": [{"theme": x[0], "items": x[1]} for x in themes]}

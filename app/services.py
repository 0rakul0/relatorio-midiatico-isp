from datetime import date, datetime
import re
import json
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse, urlunparse
from urllib.parse import urlencode
from urllib.request import urlopen
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.llm import structured_response
from app.media_scout import MediaScout, PRIORITY_WEB_PORTALS, PRIORITY_YOUTUBE_CHANNELS
from app.models import Classification, GeneratedReport, MediaItem, OfficialFact, Project, SearchQuery
from app.pdf_report import build_pdf
from app.prompts import ANALYST_PROMPT, DOCUMENTALIST_PROMPT, QUERY_PLANNER_PROMPT, WRITER_PROMPT


PRIORITY_PORTALS = [*PRIORITY_WEB_PORTALS, ("Youtube", "www.youtube.com")]
PRIORITY_DOMAINS = [domain for _, domain in PRIORITY_PORTALS]

def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    # Em URLs normais removemos parâmetros de rastreamento. No YouTube, porém,
    # o identificador do vídeo está justamente no parâmetro `v`; descartá-lo
    # faria todos os links /watch apontarem para o mesmo item do corpus.
    query = ""
    host = parsed.netloc.lower()
    if host.endswith("youtube.com") and parsed.path.rstrip("/") == "/watch":
        video_id = next((value for key, value in parse_qsl(parsed.query) if key == "v"), "")
        query = urlencode({"v": video_id}) if video_id else ""
    return urlunparse((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", query, ""))


def result_publication_date(value: object) -> date | None:
    """Converte a data publicada pela busca, quando a fonte a disponibiliza."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+03:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def publication_year(item: MediaItem) -> str:
    """Obtém o ano de fonte explícita; URLs datadas são evidência suficiente para o campo Ano."""
    inferred_date = inferred_publication_date(item)
    if inferred_date:
        return str(inferred_date.year)
    for text in (item.url, item.title):
        match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text or "")
        if match:
            return match.group(1)
    return "N/D"


def inferred_publication_date(item: MediaItem) -> date | None:
    """Usa data da fonte e, na ausência, padrões inequívocos na URL/título."""
    if item.published_at:
        return item.published_at
    for text in (item.url, item.title):
        for match in re.finditer(r"(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text or ""):
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def source_label(item: MediaItem) -> str:
    if "youtube.com" in (item.domain or "").lower():
        channel = item.source_name or (item.content or "").removeprefix("Canal: ").strip()
        return f"YouTube - {channel}" if channel else "YouTube"
    return item.domain or "Fonte aberta"


def media_window(project: Project) -> tuple[date, date]:
    """Janela auditável: lançamento (ou início manual posterior) até o fim definido."""
    start = project.collection_start
    if project.launch_date and project.launch_date > start:
        start = project.launch_date
    return start, project.collection_end


def normalized_topic_terms(text: str) -> set[str]:
    normalized = "".join(char for char in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(char))
    return {token.rstrip("s") for token in re.findall(r"[a-z0-9]+", normalized) if len(token) >= 4 and not token.isdigit()}


def normalized_text(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(char))


def mentions_named_topic(text: str, project_terms: set[str]) -> bool:
    """Exige a referência nominal quando o produto é o Dossiê Mulher."""
    normalized = normalized_text(text)
    if "mulher" in project_terms:
        return bool(re.search(r"\bdossie\s+(?:(?:da|das)\s+)?mulher(?:es)?\b", normalized))
    return bool(project_terms.intersection(normalized_topic_terms(text)))


def discover_project_profile(db: Session, project: Project) -> dict:
    """Localiza fontes e extrai perfil documental antes da série histórica."""
    has_custom_window = project.has_custom_date_window
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
    for task in MediaScout(project.topic).web_tasks():
        if task.query not in existing:
            row = SearchQuery(project_id=project.id, query=task.query, kind="media_scout_web", rationale=task.rationale, priority=2)
            db.add(row); created.append(row); existing.add(task.query)
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
        if query.kind == "youtube" and get_settings().youtube_api_key:
            query.executed_at = datetime.utcnow()
            continue
        search_params = {"query": query.query, "max_results": 10, "include_raw_content": "text", "topic": "general" if query.kind == "youtube" else "news"}
        window_start, window_end = media_window(project)
        search_params.update({"start_date": str(window_start), "end_date": str(window_end)})
        response = client.search(**search_params)
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


def collect_youtube(db: Session, project: Project) -> int:
    """Coleta metadados públicos de vídeos pela YouTube Data API v3."""
    api_key = get_settings().youtube_api_key
    if not api_key:
        return 0
    # Repara itens gravados por versões anteriores, que normalizavam todos os
    # links de vídeo para a mesma URL /watch.
    existing_youtube_items = db.scalars(select(MediaItem).where(
        MediaItem.project_id == project.id, MediaItem.search_source == "youtube_api"
    )).all()
    for item in existing_youtube_items:
        item.canonical_url = canonicalize(item.url)
    db.flush()
    scout = MediaScout(project.topic)
    scout_tasks = scout.youtube_tasks()
    window_start, window_end = media_window(project)

    results_by_video_id: dict[str, dict] = {}
    for task in scout_tasks:
        params = {
            "part": "snippet", "q": task.query, "type": "video", "order": "relevance",
            "maxResults": 15, "relevanceLanguage": "pt", "key": api_key,
        }
        params.update({
            "publishedAfter": f"{window_start.isoformat()}T00:00:00Z",
            "publishedBefore": f"{window_end.isoformat()}T23:59:59Z",
        })
        endpoint = "https://www.googleapis.com/youtube/v3/search?" + urlencode(params)
        try:
            with urlopen(endpoint, timeout=20) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Não foi possível consultar a YouTube Data API: {exc}") from exc
        for result in payload.get("items", []):
            video_id = (result.get("id") or {}).get("videoId")
            if video_id:
                results_by_video_id.setdefault(video_id, result)

    video_ids = list(results_by_video_id)
    details_by_id = youtube_video_details(api_key, video_ids)
    existing_items = {
        item.canonical_url: item for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    }
    added = 0
    for video_id, result in results_by_video_id.items():
        detail = details_by_id.get(video_id, {})
        snippet = detail.get("snippet") or result.get("snippet") or {}
        if not video_id:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        canonical = canonicalize(url)
        channel = snippet.get("channelTitle") or "YouTube"
        raw_views = (detail.get("statistics") or {}).get("viewCount")
        try:
            view_count = int(raw_views) if raw_views is not None else None
        except (TypeError, ValueError):
            view_count = None
        existing = existing_items.get(canonical)
        if existing:
            existing.title = snippet.get("title") or existing.title
            existing.published_at = result_publication_date(snippet.get("publishedAt")) or existing.published_at
            existing.snippet = snippet.get("description") or existing.snippet
            existing.content = snippet.get("description") or existing.content
            existing.source_name = channel
            existing.view_count = view_count
            existing.search_source = "youtube_api"
            added += 1
            continue
        row = MediaItem(
            project_id=project.id, title=snippet.get("title") or "Vídeo sem título", url=url,
            canonical_url=canonical, domain="youtube.com", published_at=result_publication_date(snippet.get("publishedAt")),
            snippet=snippet.get("description"), content=snippet.get("description"), source_name=channel,
            view_count=view_count, search_source="youtube_api",
        )
        db.add(row)
        existing_items[canonical] = row  # Mantém a deduplicação na mesma coleta.
        added += 1
    db.commit()
    return added


def youtube_video_details(api_key: str, video_ids: list[str]) -> dict[str, dict]:
    """Obtém visualizações e canal em lote; a API aceita até 50 IDs por consulta."""
    if not video_ids:
        return {}
    details: dict[str, dict] = {}
    for offset in range(0, len(video_ids), 50):
        endpoint = "https://www.googleapis.com/youtube/v3/videos?" + urlencode({
            "part": "snippet,statistics", "id": ",".join(video_ids[offset:offset + 50]), "key": api_key,
        })
        try:
            with urlopen(endpoint, timeout=20) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Não foi possível obter os metadados dos vídeos no YouTube: {exc}") from exc
        details.update({item.get("id"): item for item in payload.get("items", []) if item.get("id")})
    return details


def validate_and_classify(db: Session, project: Project) -> dict:
    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    valid = discarded = 0
    topic_words = set(project.topic.lower().replace('"', '').split())
    project_terms = normalized_topic_terms(project.topic)
    window_start, window_end = media_window(project)
    for item in items:
        is_social_video = item.search_source in {"youtube", "youtube_api"} or "youtube.com" in (item.domain or "").lower()
        effective_date = inferred_publication_date(item)
        # Vídeos precisam de data exata para comprovar que são posteriores ao
        # lançamento. Em sites, a ausência desse metadado não invalida por si
        # só uma matéria: Tavily frequentemente não o fornece, embora a página
        # continue sendo uma evidência auditável.
        if is_social_video and not effective_date:
            item.status, item.discard_reason = "DATE_UNVERIFIED", "Data do vídeo não verificável; não é possível confirmar que ele é posterior ao lançamento"
            discarded += 1; continue
        if effective_date and not window_start <= effective_date <= window_end:
            item.status, item.discard_reason = "OUTSIDE_COLLECTION_WINDOW", f"Publicado em {effective_date.isoformat()}, fora da janela {window_start.isoformat()} a {window_end.isoformat()}"
            discarded += 1; continue
        inferred_year = publication_year(item)
        if not is_social_video and inferred_year.isdigit() and int(inferred_year) < window_start.year:
            item.status, item.discard_reason = "OUTSIDE_COLLECTION_WINDOW", f"Ano {inferred_year} anterior ao lançamento em {window_start.isoformat()}"
            discarded += 1; continue
        body = " ".join(filter(None, [item.title, item.snippet, item.content])).lower()
        related = len(topic_words.intersection(set(body.split()))) >= 1 or "instituto de segurança pública" in body or " isp " in f" {body} "
        related = mentions_named_topic(body, project_terms)
        if item.search_source == "youtube_api":
            # Mantido explicitamente para documentar que vídeos seguem a mesma
            # regra nominal usada no corpus de sites.
            related = mentions_named_topic(body, project_terms)
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
    traditional_corpus, social_corpus = split_corpus(corpus)
    payload = {"report": result, "metrics": data, "corpus": corpus,
               "traditional_corpus": traditional_corpus, "social_corpus": social_corpus,
               "project": {"id": project.id, "topic": project.topic, "institution": project.institution, "launch_date": str(project.launch_date), "collection_start": str(project.collection_start), "collection_end": str(project.collection_end)}}
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
    payload["traditional_corpus"], payload["social_corpus"] = split_corpus(payload["corpus"])
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
        "published_year": publication_year(item),
        "source": source_label(item),
    } for item, classification in rows]


def split_corpus(corpus: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa imprensa/site de plataformas sociais, inclusive conectores futuros."""
    social_hosts = ("youtube.com", "youtu.be", "instagram.com", "x.com", "twitter.com")
    traditional, social = [], []
    for item in corpus:
        domain = (item.get("domain") or "").lower()
        target = social if any(domain == host or domain.endswith("." + host) for host in social_hosts) else traditional
        target.append(item)
    return traditional, social


def _hydrate_cached_report(db: Session, project: Project, generated: GeneratedReport) -> dict:
    payload = dict(generated.body)
    payload["project"] = {
        "id": project.id, "topic": project.topic, "institution": project.institution,
        "launch_date": str(project.launch_date), "collection_start": str(project.collection_start),
        "collection_end": str(project.collection_end),
    }
    payload["metrics"] = metrics(db, project.id)
    payload["corpus"] = corpus_for_project(db, project.id)
    payload["traditional_corpus"], payload["social_corpus"] = split_corpus(payload["corpus"])
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
    profile = discover_project_profile(db, project) if project.status in {"DRAFT", "CUSTOM_DATES"} else {
        "status": project.status, "institution": project.institution, "launch_date": str(project.launch_date),
        "collection_end": str(project.collection_end), "facts": 0, "sources": 0,
    }
    if project.status == "PROFILE_NEEDS_REVIEW":
        raise RuntimeError("Não foi possível confirmar o lançamento com evidência suficiente; revise o perfil antes da coleta.")
    planned = plan_queries_with_llm(db, project)
    collected = collect_tavily(db, project.id)
    youtube_collected = collect_youtube(db, project)
    validation = validate_and_classify(db, project)
    drafted = draft_report_with_llm(db, project)
    project.status = "REPORT_READY"
    db.commit()
    return {"project": {"id": project.id, "topic": project.topic, "institution": project.institution,
                         "launch_date": str(project.launch_date), "collection_start": str(project.collection_start),
                         "collection_end": str(project.collection_end)}, "profile": profile, "planned": len(planned),
            "collected": collected, "youtube_collected": youtube_collected, "validation": validation, **drafted}


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
    scout_status = {
        "name": "Agente de monitoramento de veículos",
        "web_tasks": len(MediaScout(project.topic).web_tasks()) if project else 0,
        "youtube_tasks": len(MediaScout(project.topic).youtube_tasks()) if project else 0,
        "platforms": MediaScout.platform_status(bool(get_settings().youtube_api_key)),
    }
    youtube_items = db.scalars(select(MediaItem).where(
        MediaItem.project_id == project_id, MediaItem.status == "VALID", MediaItem.search_source == "youtube_api"
    )).all()
    channels: dict[str, dict] = {}
    priority_channel_checks = []
    for label, channel_name in PRIORITY_YOUTUBE_CHANNELS:
        matching = [item for item in youtube_items if normalized_text(item.source_name or "") == normalized_text(channel_name)]
        views = sum(item.view_count or 0 for item in matching)
        if matching:
            lead_video = max(matching, key=lambda item: item.view_count or 0)
            priority_channel_checks.append({
                "channel": label, "videos": len(matching), "views": views,
                "result": "com cobertura auditável", "lead_title": lead_video.title,
                "lead_url": lead_video.url,
            })
    # O ranking não se limita aos veículos acompanhados nominalmente: todo canal
    # com vídeo validado sobre a pauta pode aparecer aqui.
    for item in youtube_items:
        channel = item.source_name or "Canal não identificado"
        current = channels.setdefault(channel, {
            "channel": channel, "videos": 0, "views": 0,
            "lead_title": item.title, "lead_url": item.url, "lead_views": item.view_count or 0,
        })
        current["videos"] += 1
        current["views"] += item.view_count or 0
        if (item.view_count or 0) > current["lead_views"]:
            current.update({"lead_title": item.title, "lead_url": item.url, "lead_views": item.view_count or 0})
    top_youtube_channels = sorted(channels.values(), key=lambda row: (row["views"], row["videos"]), reverse=True)[:5]
    top_youtube_videos = [{
        "title": item.title, "channel": item.source_name or "Canal não identificado", "views": item.view_count,
        "published_year": publication_year(item), "url": item.url,
    } for item in sorted(youtube_items, key=lambda item: item.view_count or 0, reverse=True)[:5]]
    return {"items_found": total, "valid_items": valid, "discarded_items": total - valid, "unique_vehicles": vehicles,
            "collection_days": collection_days, "isp_mentioned_items": isp,
            "isp_protagonism_percent": round((isp / valid * 100), 1) if valid else 0,
            "themes": [{"theme": x[0], "items": x[1]} for x in themes], "portal_checks": portal_checks,
            "media_scout": scout_status,
            "youtube_videos": len(youtube_items), "youtube_priority_channel_checks": priority_channel_checks,
            "top_youtube_channels": top_youtube_channels,
            "top_youtube_videos": top_youtube_videos}

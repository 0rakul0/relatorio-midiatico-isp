from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.fact_layer import (
    extract_project_facts,
    fact_assertions_for_report,
    fact_event_validation_summary,
    fact_events_for_main_report,
    plan_nominal_followups,
    resolve_project_facts,
)
from app.llm import llm_is_configured, structured_response
from app.media_scout import MediaScout
from app.models import Classification, GeneratedReport, MediaItem, OfficialFact, Project, SearchQuery
from app.pdf_report import build_pdf
from app.prompts import ANALYST_PROMPT, DOCUMENTALIST_PROMPT, MEDIA_RELEVANCE_PROMPT, QUERY_PLANNER_PROMPT, WRITER_PROMPT
from app.report_qa import run_report_qa
from app.source_registry import OFFICIAL_SECURITY_SOURCES, PRIORITY_MEDIA_SOURCES, PRIORITY_YOUTUBE_CHANNELS
from app.topic_profile import build_topic_profile, normalized_terms, normalized_text, requested_month_window


PRIORITY_PORTALS = [*PRIORITY_MEDIA_SOURCES, ("YouTube", "youtube.com")]


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    query = ""
    if host.endswith("youtube.com") and parsed.path.rstrip("/") == "/watch":
        video_id = next((value for key, value in parse_qsl(parsed.query) if key == "v"), "")
        query = urlencode({"v": video_id}) if video_id else ""
    return urlunparse((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", query, ""))


def result_publication_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def inferred_publication_date(item: MediaItem) -> date | None:
    if item.published_at:
        return item.published_at
    for text_value in (item.url, item.title):
        for match in re.finditer(r"(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text_value or ""):
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def publication_year(item: MediaItem) -> str:
    inferred = inferred_publication_date(item)
    if inferred:
        return str(inferred.year)
    for text_value in (item.url, item.title):
        match = re.search(r"(?<!\d)(20\d{2})(?!\d)", text_value or "")
        if match:
            return match.group(1)
    return "N/D"


def source_label(item: MediaItem) -> str:
    host = (item.domain or "").lower()
    if "youtube.com" in host:
        return f"YouTube - {item.source_name}" if item.source_name else "YouTube"
    return item.source_name or item.domain or "Fonte aberta"


def media_window(project: Project) -> tuple[date, date]:
    return project.collection_start, project.collection_end


def query_window(project: Project, query: SearchQuery) -> tuple[date | None, date | None]:
    if query.purpose == "OFFICIAL_FACT":
        return None, None
    if query.purpose in {"FACT_DISCOVERY", "NOMINAL_FOLLOWUP"}:
        start = project.event_start or project.collection_start
        end = project.event_end or project.collection_end
        return start, end + timedelta(days=project.fact_grace_days or 0)
    return project.collection_start, project.collection_end


def _project_payload(project: Project) -> dict:
    return {
        "id": project.id,
        "topic": project.topic,
        "institution": project.institution,
        "project_type": project.project_type,
        "launch_date": project.launch_date.isoformat() if project.launch_date else None,
        "collection_start": project.collection_start.isoformat(),
        "collection_end": project.collection_end.isoformat(),
        "event_start": project.event_start.isoformat() if project.event_start else None,
        "event_end": project.event_end.isoformat() if project.event_end else None,
        "fact_grace_days": project.fact_grace_days,
    }


def discover_project_profile(db: Session, project: Project) -> dict:
    profile = build_topic_profile(project.topic)
    project.project_type = profile["project_type"]
    project.topic_profile = profile
    if project.event_start is None:
        project.event_start = project.collection_start
    if project.event_end is None:
        project.event_end = project.collection_end

    if project.project_type != "INSTITUTIONAL_PRODUCT":
        project.status = "PROFILED"
        db.commit()
        return {
            "status": project.status,
            "project_type": project.project_type,
            "topic_profile": project.topic_profile,
            "launch_required": False,
            "facts": 0,
            "sources": 0,
        }

    key = get_settings().tavily_api_key
    if not key:
        project.status = "PROFILE_NEEDS_REVIEW"
        db.commit()
        return {
            "status": project.status,
            "project_type": project.project_type,
            "warning": "TAVILY_API_KEY não configurada para confirmar o produto institucional",
            "launch_required": True,
        }

    from tavily import TavilyClient

    client = TavilyClient(api_key=key)
    searches = [
        f'"{project.topic}" "Instituto de Segurança Pública"',
        f'site:isp.rj.gov.br "{project.topic}"',
        f'"{project.topic}" lançamento',
    ]
    sources: list[dict] = []
    seen: set[str] = set()
    for query in searches:
        try:
            response = client.search(
                query=query,
                max_results=5,
                include_raw_content="text",
                search_depth="advanced",
                timeout=15,
            )
        except Exception:
            continue
        for result in response.get("results", []):
            url = result.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(
                {
                    "title": result.get("title", "Sem título"),
                    "url": url,
                    "content": (result.get("raw_content") or result.get("content") or "")[:2500],
                }
            )
    sources = sources[:8]
    if not sources or not llm_is_configured():
        project.status = "PROFILE_NEEDS_REVIEW"
        db.commit()
        return {
            "status": project.status,
            "project_type": project.project_type,
            "sources": len(sources),
            "facts": 0,
            "launch_required": True,
            "warning": "Não foi possível confirmar o lançamento com evidência suficiente",
        }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "institution": {"type": ["string", "null"]},
            "launch_date": {"type": ["string", "null"]},
            "official_facts": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "value": {"type": "string"},
                        "evidence": {"type": "string"},
                        "source_index": {"type": "integer", "minimum": 0},
                        "indicator": {"type": ["string", "null"]},
                        "geography": {"type": ["string", "null"]},
                        "period_start": {"type": ["string", "null"]},
                        "period_end": {"type": ["string", "null"]},
                        "unit": {"type": ["string", "null"]},
                    },
                    "required": [
                        "label", "value", "evidence", "source_index", "indicator", "geography",
                        "period_start", "period_end", "unit",
                    ],
                },
            },
        },
        "required": ["institution", "launch_date", "official_facts"],
    }
    result = structured_response(
        instructions=DOCUMENTALIST_PROMPT,
        payload={"topic": project.topic, "sources": sources},
        schema_name="institutional_product_profile",
        schema=schema,
    )

    if result.get("institution"):
        project.institution = result["institution"][:200]
    launch_confirmed = False
    if result.get("launch_date"):
        try:
            parsed = date.fromisoformat(result["launch_date"][:10])
            project.launch_date = parsed
            launch_confirmed = True
        except ValueError:
            launch_confirmed = False

    facts_added = 0
    for fact in result.get("official_facts", []):
        source_index = fact["source_index"]
        if source_index >= len(sources):
            continue
        source = sources[source_index]
        exists = db.scalar(
            select(OfficialFact.id).where(
                OfficialFact.project_id == project.id,
                OfficialFact.label == fact["label"],
                OfficialFact.evidence == fact["evidence"],
            )
        )
        if exists:
            continue
        period_start = result_publication_date(fact.get("period_start"))
        period_end = result_publication_date(fact.get("period_end"))
        db.add(
            OfficialFact(
                project_id=project.id,
                label=fact["label"][:250],
                value=fact["value"],
                source_reference=source["url"][:500],
                evidence=fact["evidence"],
                page=None,
                indicator=fact.get("indicator"),
                geography=fact.get("geography"),
                period_start=period_start,
                period_end=period_end,
                unit=fact.get("unit"),
            )
        )
        facts_added += 1

    project.status = "PROFILED" if launch_confirmed else "PROFILE_NEEDS_REVIEW"
    db.commit()
    return {
        "status": project.status,
        "project_type": project.project_type,
        "sources": len(sources),
        "facts": facts_added,
        "launch_date_confirmed": launch_confirmed,
    }


def _existing_queries(db: Session, project_id: int) -> set[str]:
    return set(db.scalars(select(SearchQuery.query).where(SearchQuery.project_id == project_id)).all())


def _add_query(
    db: Session,
    project: Project,
    existing: set[str],
    *,
    query: str,
    kind: str,
    purpose: str,
    rationale: str,
    priority: int,
) -> SearchQuery | None:
    query = " ".join(query.split()).strip()
    if not query or query in existing:
        return None
    row = SearchQuery(
        project_id=project.id,
        query=query,
        kind=kind,
        purpose=purpose,
        rationale=rationale,
        priority=priority,
    )
    db.add(row)
    existing.add(query)
    return row


def _fact_query_bases(project: Project) -> list[str]:
    profile = project.topic_profile or {}
    actors = (profile.get("actors") or [])[:5]
    actions = (profile.get("actions") or [])[:5]
    locations = (profile.get("locations") or [])[:3]
    bases: list[str] = [project.topic]
    if actors and actions:
        for actor in actors[:5]:
            for action in actions[:4]:
                parts = [actor, action, *locations[:1]]
                bases.append(" ".join(part for part in parts if part))
    bases.extend((profile.get("search_synonyms") or [])[:8])
    return list(dict.fromkeys(base.strip() for base in bases if base.strip()))[:24]


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    if not project.topic_profile:
        project.topic_profile = build_topic_profile(project.topic)
        project.project_type = project.topic_profile["project_type"]

    existing = _existing_queries(db, project.id)
    created: list[SearchQuery] = []

    for task in MediaScout(project.topic, project.topic_profile).web_tasks():
        row = _add_query(
            db,
            project,
            existing,
            query=task.query,
            kind="media_scout_web",
            purpose="MEDIA_REPERCUSSION",
            rationale=task.rationale,
            priority=2,
        )
        if row:
            created.append(row)

    if project.project_type == "EVENT_TOPIC":
        bases = _fact_query_bases(project)
        for base in bases:
            row = _add_query(
                db,
                project,
                existing,
                query=base,
                kind="fact_discovery",
                purpose="FACT_DISCOVERY",
                rationale="Descobrir ocorrências individuais e fontes que documentem o fato",
                priority=1,
            )
            if row:
                created.append(row)

        compact = bases[1] if len(bases) > 1 else project.topic
        for source in OFFICIAL_SECURITY_SOURCES:
            row = _add_query(
                db,
                project,
                existing,
                query=f'site:{source["domain"]} {compact}',
                kind="official",
                purpose="OFFICIAL_FACT",
                rationale=f'Buscar confirmação institucional em {source["label"]}',
                priority=1,
            )
            if row:
                created.append(row)

    db.commit()
    return created


def plan_queries_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    deterministic = plan_queries(db, project)
    if not llm_is_configured():
        return deterministic

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "queries": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string"},
                        "kind": {"type": "string"},
                        "purpose": {
                            "type": "string",
                            "enum": ["MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"],
                        },
                        "rationale": {"type": "string"},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                    "required": ["query", "kind", "purpose", "rationale", "priority"],
                },
            }
        },
        "required": ["queries"],
    }
    result = structured_response(
        instructions=QUERY_PLANNER_PROMPT,
        payload={
            "project": _project_payload(project),
            "topic_profile": project.topic_profile,
            "official_facts": [
                {
                    "label": fact.label,
                    "value": fact.value,
                    "indicator": fact.indicator,
                    "geography": fact.geography,
                    "period_start": fact.period_start.isoformat() if fact.period_start else None,
                    "period_end": fact.period_end.isoformat() if fact.period_end else None,
                    "source_reference": fact.source_reference,
                    "evidence": fact.evidence,
                }
                for fact in facts
            ],
        },
        schema_name="search_plan_v2",
        schema=schema,
    )

    existing = _existing_queries(db, project.id)
    created = list(deterministic)
    for item in result.get("queries", []):
        row = _add_query(
            db,
            project,
            existing,
            query=item["query"],
            kind=item["kind"][:40],
            purpose=item["purpose"],
            rationale=item["rationale"],
            priority=item["priority"],
        )
        if row:
            created.append(row)
    db.commit()
    return created


def _append_purpose(item: MediaItem, purpose: str) -> None:
    purposes = list(item.discovery_purposes or [])
    if purpose not in purposes:
        purposes.append(purpose)
        item.discovery_purposes = purposes


def collect_tavily(
    db: Session,
    project_id: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> int:
    key = get_settings().tavily_api_key
    if not key:
        raise RuntimeError("TAVILY_API_KEY não configurada")

    from tavily import TavilyClient

    client = TavilyClient(api_key=key)
    project = db.get(Project, project_id)
    if not project:
        raise RuntimeError("Projeto não encontrado")

    existing_items = {
        item.canonical_url: item
        for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project_id)).all()
    }
    queries = db.scalars(
        select(SearchQuery)
        .where(SearchQuery.project_id == project_id, SearchQuery.executed_at.is_(None))
        .order_by(SearchQuery.priority.asc(), SearchQuery.id.asc())
    ).all()

    added = 0
    total_queries = len(queries)
    for query_index, query in enumerate(queries, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Consulta {query_index}/{total_queries}: {query.query[:100]}")
        if query.kind == "youtube" and get_settings().youtube_api_key:
            query.executed_at = datetime.utcnow()
            continue

        start, end = query_window(project, query)
        params: dict = {
            "query": query.query,
            "max_results": get_settings().max_search_results,
            "include_raw_content": "text",
            "topic": "news" if query.purpose == "MEDIA_REPERCUSSION" else "general",
            "search_depth": "advanced" if query.purpose != "MEDIA_REPERCUSSION" else "basic",
        }
        if start:
            params["start_date"] = start.isoformat()
        if end:
            params["end_date"] = end.isoformat()

        try:
            response = client.search(**params)
        except Exception as exc:
            query.executed_at = datetime.utcnow()
            db.commit()
            raise RuntimeError(f"Falha na busca Tavily para '{query.query}': {exc}") from exc

        for result in response.get("results", []):
            url = result.get("url")
            if not url:
                continue
            canonical = canonicalize(url)
            existing = existing_items.get(canonical)
            if existing:
                _append_purpose(existing, query.purpose)
                if not existing.content and result.get("raw_content"):
                    existing.content = result.get("raw_content")
                if not existing.snippet and result.get("content"):
                    existing.snippet = result.get("content")
                if not existing.published_at:
                    existing.published_at = result_publication_date(result.get("published_date"))
                continue

            row = MediaItem(
                project_id=project_id,
                query_id=query.id,
                title=result.get("title") or "Sem título",
                url=url,
                canonical_url=canonical,
                domain=urlparse(url).netloc.lower(),
                published_at=result_publication_date(result.get("published_date")),
                snippet=result.get("content"),
                content=result.get("raw_content"),
                source_name=result.get("source"),
                search_source="tavily",
                discovery_purposes=[query.purpose],
            )
            db.add(row)
            existing_items[canonical] = row
            added += 1
        query.executed_at = datetime.utcnow()
        db.commit()
    return added


def collect_youtube(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> int:
    api_key = get_settings().youtube_api_key
    if not api_key:
        return 0

    tasks = MediaScout(project.topic, project.topic_profile).youtube_tasks()
    start, end = media_window(project)
    results_by_video_id: dict[str, dict] = {}

    total_tasks = len(tasks)
    for task_index, task in enumerate(tasks, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Busca {task_index}/{total_tasks} no YouTube: {task.target}")
        params = {
            "part": "snippet",
            "q": task.query,
            "type": "video",
            "order": "relevance",
            "maxResults": 15,
            "relevanceLanguage": "pt",
            "key": api_key,
            "publishedAfter": f"{start.isoformat()}T00:00:00Z",
            "publishedBefore": f"{end.isoformat()}T23:59:59Z",
        }
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

    details_by_id = youtube_video_details(api_key, list(results_by_video_id))
    existing_items = {
        item.canonical_url: item
        for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    }
    added = 0
    for video_id, result in results_by_video_id.items():
        detail = details_by_id.get(video_id, {})
        snippet = detail.get("snippet") or result.get("snippet") or {}
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
            _append_purpose(existing, "MEDIA_REPERCUSSION")
            continue

        row = MediaItem(
            project_id=project.id,
            title=snippet.get("title") or "Vídeo sem título",
            url=url,
            canonical_url=canonical,
            domain="youtube.com",
            published_at=result_publication_date(snippet.get("publishedAt")),
            snippet=snippet.get("description"),
            content=snippet.get("description"),
            source_name=channel,
            view_count=view_count,
            search_source="youtube_api",
            discovery_purposes=["MEDIA_REPERCUSSION"],
        )
        db.add(row)
        existing_items[canonical] = row
        added += 1
    db.commit()
    return added


def collect_media_agents(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    web_progress: Callable[[str], None] | None = None,
    youtube_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Executa coletores independentes e devolve o resultado de cada agente.

    Em PostgreSQL, web e YouTube usam sessões próprias em paralelo. Em SQLite,
    as gravações são sequenciais para não bloquear o arquivo local; os agentes,
    seus resultados e o tratamento isolado de falhas continuam separados.
    """

    def web_agent() -> int:
        session = SessionLocal()
        try:
            return collect_tavily(
                session,
                project.id,
                cancel_check=cancel_check,
                progress_detail=web_progress,
            )
        finally:
            session.close()

    def youtube_agent() -> dict[str, object]:
        if not get_settings().youtube_api_key:
            return {"status": "NOT_CONFIGURED", "collected": 0, "error": None}
        session = SessionLocal()
        try:
            local_project = session.get(Project, project.id)
            if not local_project:
                raise RuntimeError("Projeto não encontrado")
            collected = collect_youtube(
                session,
                local_project,
                cancel_check=cancel_check,
                progress_detail=youtube_progress,
            )
            return {"status": "COMPLETED", "collected": collected, "error": None}
        except RuntimeError as exc:
            return {"status": "UNAVAILABLE", "collected": 0, "error": str(exc)[:1000]}
        finally:
            session.close()

    dialect_name = db.bind.dialect.name if db.bind is not None else ""
    parallel = dialect_name != "sqlite"
    if parallel:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-agent") as executor:
            web_future = executor.submit(web_agent)
            youtube_future = executor.submit(youtube_agent)
            web_collected = web_future.result()
            youtube = youtube_future.result()
    else:
        web_collected = web_agent()
        youtube = youtube_agent()

    return {
        "web": {"status": "COMPLETED", "collected": web_collected},
        "youtube": youtube,
        "parallel": parallel,
    }


def youtube_video_details(api_key: str, video_ids: list[str]) -> dict[str, dict]:
    if not video_ids:
        return {}
    details: dict[str, dict] = {}
    for offset in range(0, len(video_ids), 50):
        endpoint = "https://www.googleapis.com/youtube/v3/videos?" + urlencode(
            {
                "part": "snippet,statistics",
                "id": ",".join(video_ids[offset : offset + 50]),
                "key": api_key,
            }
        )
        try:
            with urlopen(endpoint, timeout=20) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Não foi possível obter metadados do YouTube: {exc}") from exc
        details.update({item.get("id"): item for item in payload.get("items", []) if item.get("id")})
    return details


def _topic_overlap_score(project: Project, body: str) -> int:
    profile = project.topic_profile or {}
    haystack = normalized_text(body)
    groups = [profile.get("actors") or [], profile.get("actions") or [], profile.get("locations") or []]
    score = 0
    for group in groups:
        if group and any(normalized_text(term) in haystack for term in group):
            score += 1
    if score == 0:
        topic_terms = normalized_terms(project.topic)
        body_terms = normalized_terms(body)
        overlap = topic_terms.intersection(body_terms)
        score = 1 if len(overlap) >= max(1, min(3, len(topic_terms) // 3)) else 0
    return score


def _review_media_relevance(project: Project, item: MediaItem) -> tuple[bool, str]:
    body = " ".join(filter(None, [item.title, item.snippet, (item.content or "")[:12000]]))
    fallback_score = _topic_overlap_score(project, body)
    if not llm_is_configured():
        return fallback_score > 0, "Aderência lexical" if fallback_score > 0 else "Sem aderência lexical suficiente"

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "related": {"type": "boolean"},
            "evidence": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["related", "evidence", "reason"],
    }
    try:
        result = structured_response(
            instructions=MEDIA_RELEVANCE_PROMPT,
            payload={
                "topic": project.topic,
                "topic_profile": project.topic_profile,
                "item": {
                    "title": item.title,
                    "snippet": item.snippet,
                    "content": (item.content or "")[:12000],
                },
            },
            schema_name="media_relevance",
            schema=schema,
        )
        return bool(result["related"]), result["evidence"] or result["reason"]
    except RuntimeError:
        return fallback_score > 0, "Fallback lexical após indisponibilidade da revisão semântica"


def validate_and_classify(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    start, end = media_window(project)
    valid = discarded = semantic_reviews = 0

    total_items = len(items)
    for item_index, item in enumerate(items, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Item {item_index}/{total_items}: {item.title[:90]}")
        publication_date = inferred_publication_date(item)
        is_social = item.search_source in {"youtube", "youtube_api"} or "youtube.com" in (item.domain or "").lower()

        if is_social and not publication_date:
            item.status = "DATE_UNVERIFIED"
            item.discard_reason = "Data de publicação do vídeo não verificável"
            discarded += 1
            continue
        if publication_date and not start <= publication_date <= end:
            item.status = "OUTSIDE_COLLECTION_WINDOW"
            item.discard_reason = (
                f"Publicado em {publication_date.isoformat()}, fora da janela midiática "
                f"{start.isoformat()} a {end.isoformat()}"
            )
            discarded += 1
            continue

        semantic_reviews += 1
        related, evidence = _review_media_relevance(project, item)
        if not related:
            item.status = "NOT_RELATED"
            item.discard_reason = evidence[:1000]
            discarded += 1
            continue

        item.status = "VALID"
        item.discard_reason = None
        mention = "instituto de segurança pública" in normalized_text(" ".join(filter(None, [item.title, item.snippet, item.content])))
        classification = db.scalar(select(Classification).where(Classification.media_item_id == item.id))
        if not classification:
            db.add(
                Classification(
                    media_item_id=item.id,
                    theme="Geral",
                    framing="A determinar por revisão analítica",
                    isp_mentioned=mention,
                    tone_toward_institution="NEUTRO",
                    fidelity_status="PENDENTE",
                    evidence=(evidence or item.snippet or item.title)[:1000],
                    errors=[],
                )
            )
        valid += 1

    db.commit()
    return {"valid": valid, "discarded": discarded, "semantic_reviews": semantic_reviews}


def classify_with_llm(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    if not llm_is_configured():
        return {"updated": 0, "skipped": True}

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    resolved_facts = fact_events_for_main_report(db, project.id)
    items = db.scalars(
        select(MediaItem).where(MediaItem.project_id == project.id, MediaItem.status == "VALID")
    ).all()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "theme": {"type": "string"},
            "framing": {"type": "string"},
            "isp_mentioned": {"type": "boolean"},
            "tone_toward_institution": {
                "type": "string",
                "enum": ["POSITIVO", "NEUTRO", "NEGATIVO", "INVERIFICÁVEL"],
            },
            "fidelity_status": {"type": "string", "enum": ["FIEL", "DIVERGENTE", "INVERIFICÁVEL"]},
            "evidence": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "theme", "framing", "isp_mentioned", "tone_toward_institution",
            "fidelity_status", "evidence", "errors",
        ],
    }
    official_facts = [
        {
            "label": fact.label,
            "value": fact.value,
            "evidence": fact.evidence,
            "indicator": fact.indicator,
            "geography": fact.geography,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat() if fact.period_end else None,
        }
        for fact in facts
    ]

    updated = 0
    total_items = len(items)
    for item_index, item in enumerate(items, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Classificando {item_index}/{total_items}: {item.title[:90]}")
        result = structured_response(
            instructions=ANALYST_PROMPT,
            payload={
                "official_facts": official_facts,
                "resolved_facts": resolved_facts,
                "media_item": {
                    "id": item.id,
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "content": (item.content or "")[:12000],
                },
            },
            schema_name="media_classification",
            schema=schema,
        )
        classification = db.scalar(select(Classification).where(Classification.media_item_id == item.id))
        if not classification:
            classification = Classification(media_item_id=item.id, **result)
            db.add(classification)
        else:
            for field, value in result.items():
                setattr(classification, field, value)
        updated += 1
    db.commit()
    return {"updated": updated, "skipped": False}


def corpus_for_project(db: Session, project_id: int) -> list[dict]:
    rows = db.execute(
        select(MediaItem, Classification)
        .outerjoin(Classification, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.desc().nullslast(), MediaItem.id.desc())
    ).all()
    return [
        {
            "title": item.title,
            "url": item.url,
            "domain": item.domain,
            "theme": classification.theme if classification else None,
            "evidence": classification.evidence if classification else None,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "published_year": publication_year(item),
            "source": source_label(item),
            "view_count": item.view_count,
        }
        for item, classification in rows
    ]


def split_corpus(corpus: list[dict]) -> tuple[list[dict], list[dict]]:
    social_hosts = ("youtube.com", "youtu.be", "instagram.com", "x.com", "twitter.com")
    traditional: list[dict] = []
    social: list[dict] = []
    for item in corpus:
        domain = (item.get("domain") or "").lower()
        target = social if any(domain == host or domain.endswith("." + host) for host in social_hosts) else traditional
        target.append(item)
    return traditional, social


def metrics(db: Session, project_id: int) -> dict:
    total = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id)) or 0
    valid = db.scalar(
        select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
    ) or 0
    vehicles = db.scalar(
        select(func.count(func.distinct(MediaItem.domain))).where(
            MediaItem.project_id == project_id, MediaItem.status == "VALID"
        )
    ) or 0
    isp = db.scalar(
        select(func.count(Classification.id))
        .join(MediaItem)
        .where(
            MediaItem.project_id == project_id,
            MediaItem.status == "VALID",
            Classification.isp_mentioned.is_(True),
        )
    ) or 0
    themes = db.execute(
        select(Classification.theme, func.count(Classification.id).label("items"))
        .join(MediaItem)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .group_by(Classification.theme)
        .order_by(func.count(Classification.id).desc())
    ).all()

    domains = [
        domain.lower()
        for domain in db.scalars(
            select(MediaItem.domain).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        ).all()
        if domain
    ]
    project = db.get(Project, project_id)
    youtube_status = project.youtube_collection_status if project else "NOT_ATTEMPTED"
    youtube_error = project.youtube_collection_error if project else None
    youtube_unavailable = youtube_status == "UNAVAILABLE"
    portal_checks = []
    for portal, domain in PRIORITY_PORTALS:
        found = sum(item == domain or item.endswith("." + domain) for item in domains)
        if portal == "YouTube" and youtube_unavailable:
            result = "coleta indisponível nesta execução"
            evidence = (
                f"{found} item(ns) de coletas anteriores foram preservados; a indisponibilidade não indica ausência de cobertura."
                if found
                else "A indisponibilidade da API não permite concluir ausência de cobertura no YouTube."
            )
        else:
            result = "com cobertura auditável" if found else "sem item validado na amostra"
            evidence = (
                f"{found} item(ns) validado(s) no domínio da amostra."
                if found
                else "Nenhum item validado nesse domínio no corpus coletado."
            )
        portal_checks.append(
            {
                "portal": portal,
                "result": result,
                "evidence": evidence,
            }
        )

    collection_days = ((project.collection_end - project.collection_start).days + 1) if project else 0
    youtube_note = None
    if youtube_unavailable:
        youtube_note = "YouTube indisponível nesta execução; isso não representa ausência de cobertura na plataforma."
    elif youtube_status == "NOT_CONFIGURED":
        youtube_note = "YouTube não foi consultado porque a chave da API não está configurada."
    platforms = MediaScout.platform_status(bool(get_settings().youtube_api_key))
    for platform in platforms:
        if platform["platform"] != "YouTube":
            continue
        if youtube_unavailable:
            platform["status"] = "indisponível nesta execução"
        elif youtube_status == "NOT_CONFIGURED":
            platform["status"] = "não configurado"

    scout_status = {
        "name": "Agente de monitoramento de veículos",
        "web_tasks": len(MediaScout(project.topic, project.topic_profile).web_tasks()) if project else 0,
        "youtube_tasks": len(MediaScout(project.topic, project.topic_profile).youtube_tasks()) if project else 0,
        "platforms": platforms,
    }

    youtube_items = db.scalars(
        select(MediaItem).where(
            MediaItem.project_id == project_id,
            MediaItem.status == "VALID",
            MediaItem.search_source == "youtube_api",
        )
    ).all()
    channels: dict[str, dict] = {}
    priority_channel_checks = []
    for label, channel_name in PRIORITY_YOUTUBE_CHANNELS:
        matching = [
            item for item in youtube_items if normalized_text(item.source_name or "") == normalized_text(channel_name)
        ]
        if not matching:
            continue
        views = sum(item.view_count or 0 for item in matching)
        lead = max(matching, key=lambda item: item.view_count or 0)
        priority_channel_checks.append(
            {
                "channel": label,
                "videos": len(matching),
                "views": views,
                "result": "com cobertura auditável",
                "lead_title": lead.title,
                "lead_url": lead.url,
            }
        )

    for item in youtube_items:
        channel = item.source_name or "Canal não identificado"
        current = channels.setdefault(
            channel,
            {
                "channel": channel,
                "videos": 0,
                "views": 0,
                "lead_title": item.title,
                "lead_url": item.url,
                "lead_views": item.view_count or 0,
            },
        )
        current["videos"] += 1
        current["views"] += item.view_count or 0
        if (item.view_count or 0) > current["lead_views"]:
            current.update(
                {"lead_title": item.title, "lead_url": item.url, "lead_views": item.view_count or 0}
            )

    top_youtube_channels = sorted(
        channels.values(), key=lambda row: (row["views"], row["videos"]), reverse=True
    )[:5]
    top_youtube_videos = [
        {
            "title": item.title,
            "channel": item.source_name or "Canal não identificado",
            "views": item.view_count,
            "published_year": publication_year(item),
            "url": item.url,
        }
        for item in sorted(youtube_items, key=lambda item: item.view_count or 0, reverse=True)[:5]
    ]

    facts = fact_events_for_main_report(db, project_id)
    fact_validation = fact_event_validation_summary(db, project_id)
    return {
        "items_found": total,
        "valid_items": valid,
        "discarded_items": total - valid,
        "unique_vehicles": vehicles,
        "collection_days": collection_days,
        "isp_mentioned_items": isp,
        "isp_protagonism_percent": round((isp / valid * 100), 1) if valid else 0,
        "themes": [{"theme": row[0], "items": row[1]} for row in themes],
        "portal_checks": portal_checks,
        "media_scout": scout_status,
        "youtube_videos": len(youtube_items),
        "youtube_collection_status": youtube_status,
        "youtube_collection_note": youtube_note,
        "youtube_collection_error": youtube_error,
        "youtube_priority_channel_checks": priority_channel_checks,
        "top_youtube_channels": top_youtube_channels,
        "top_youtube_videos": top_youtube_videos,
        "facts": {
            "events": len(facts),
            "confirmed": sum(item["resolution_status"] == "CONFIRMED" for item in facts),
            "partial": sum(item["resolution_status"] == "PARTIALLY_CONFIRMED" for item in facts),
            "conflicts": sum(item["resolution_status"] == "SOURCE_CONFLICT" for item in facts),
            "excluded_from_main_report": fact_validation["excluded"],
            "exclusion_reasons": fact_validation["excluded_by_reason"],
        },
    }


def draft_report_with_llm(db: Session, project: Project) -> dict:
    if not llm_is_configured():
        raise RuntimeError("Uma chave de LLM é necessária para redigir o relatório")

    data = metrics(db, project.id)
    items = db.execute(
        select(MediaItem, Classification)
        .join(Classification, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project.id, MediaItem.status == "VALID")
    ).all()
    official_facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    fact_events = fact_events_for_main_report(db, project.id)
    fact_evidence = fact_assertions_for_report(db, project.id, main_report_only=True)

    report_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "interpretive_title": {"type": "string"},
            "subtitle": {"type": "string"},
            "executive_summary": {"type": "string"},
            "fact_layer_intro": {"type": "string"},
            "opening": {"type": "string"},
            "panorama": {"type": "string"},
            "dominant_framing": {"type": "string"},
            "highest_yield": {"type": "string"},
            "institutional_narrative": {"type": "string"},
            "synthesis": {"type": "string"},
            "methodological_note": {"type": "string"},
            "thematic_axes": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "axis": {"type": "string"},
                        "anchor_data": {"type": "string"},
                        "coverage": {"type": "string"},
                    },
                    "required": ["axis", "anchor_data", "coverage"],
                },
            },
            "risk_assessment": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "dimension": {"type": "string"},
                        "assessment": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["dimension", "assessment", "evidence"],
                },
            },
            "recommendations": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
            "press_kit": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "product": {"type": "string"},
                        "purpose": {"type": "string"},
                    },
                    "required": ["product", "purpose"],
                },
            },
        },
        "required": [
            "title", "interpretive_title", "subtitle", "executive_summary", "fact_layer_intro", "opening",
            "panorama", "dominant_framing", "thematic_axes", "highest_yield", "institutional_narrative",
            "risk_assessment", "recommendations", "press_kit", "synthesis", "methodological_note",
        ],
    }

    result = structured_response(
        instructions=WRITER_PROMPT
        + " Estruture o texto nas seções fornecidas pelo schema. A tabela factual será renderizada diretamente pelo sistema; "
        + "fact_layer_intro deve apenas contextualizá-la, sem reescrever ou alterar os fatos.",
        payload={
            "project": _project_payload(project),
            "metrics": data,
            "official_facts": [
                {
                    "label": fact.label,
                    "value": fact.value,
                    "evidence": fact.evidence,
                    "source": fact.source_reference,
                    "indicator": fact.indicator,
                    "geography": fact.geography,
                    "period_start": fact.period_start.isoformat() if fact.period_start else None,
                    "period_end": fact.period_end.isoformat() if fact.period_end else None,
                    "unit": fact.unit,
                }
                for fact in official_facts
            ],
            "fact_events": fact_events,
            "validated_items": [
                {
                    "title": item.title,
                    "url": item.url,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                    "evidence": classification.evidence,
                    "theme": classification.theme,
                    "framing": classification.framing,
                }
                for item, classification in items
            ],
        },
        schema_name="structured_media_report_v2",
        schema=report_schema,
        max_output_tokens=7000,
    )

    corpus = corpus_for_project(db, project.id)
    traditional, social = split_corpus(corpus)
    payload = {
        "report": result,
        "metrics": data,
        "corpus": corpus,
        "traditional_corpus": traditional,
        "social_corpus": social,
        "fact_events": fact_events,
        "fact_evidence": fact_evidence,
        "project": _project_payload(project),
    }

    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if saved:
        saved.body = payload
        saved.qa_status = "PENDING"
        saved.qa_findings = []
    else:
        db.add(GeneratedReport(project_id=project.id, body=payload, qa_status="PENDING", qa_findings=[]))
    db.commit()
    return payload


def _hydrate_cached_report(db: Session, project: Project, generated: GeneratedReport) -> dict:
    payload = dict(generated.body)
    payload["project"] = _project_payload(project)
    payload["metrics"] = metrics(db, project.id)
    payload["corpus"] = corpus_for_project(db, project.id)
    payload["traditional_corpus"], payload["social_corpus"] = split_corpus(payload["corpus"])
    payload["fact_events"] = fact_events_for_main_report(db, project.id)
    payload["fact_evidence"] = fact_assertions_for_report(db, project.id, main_report_only=True)
    payload["qa"] = {"status": generated.qa_status, "findings": generated.qa_findings or []}
    payload["cached_at"] = generated.generated_at.isoformat() if generated.generated_at else None
    return payload


def cached_report_for_topic(
    db: Session,
    topic: str,
    collection_start: date | None = None,
    collection_end: date | None = None,
) -> dict | None:
    statement = (
        select(Project, GeneratedReport)
        .join(GeneratedReport, GeneratedReport.project_id == Project.id)
        .where(func.lower(Project.topic) == topic.strip().casefold())
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


def export_report_pdf(db: Session, project: Project, *, allow_draft: bool = False) -> bytes:
    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if not saved:
        raise RuntimeError("Relatório ainda não foi gerado")
    if not allow_draft and saved.qa_status != "APPROVED":
        raise RuntimeError(
            f"O relatório não passou pelo QA final (status: {saved.qa_status}). Use export-draft.pdf apenas para revisão interna."
        )
    payload = _hydrate_cached_report(db, project, saved)
    return build_pdf(payload)


def run_full_methodology(
    db: Session,
    project: Project,
    *,
    progress_callback: Callable[[str, str, str | None], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """Executa a metodologia completa com progresso por etapa e cancelamento cooperativo."""

    def check() -> None:
        if cancel_check:
            cancel_check()

    def stage(key: str, status: str, detail: str | None = None) -> None:
        if progress_callback:
            progress_callback(key, status, detail)

    def detail_for(key: str) -> Callable[[str], None]:
        return lambda detail: stage(key, "RUNNING", detail)

    check()
    stage("profile", "RUNNING", "Classificando o tipo de pauta e estruturando o perfil do tema")
    if project.status in {"DRAFT", "CUSTOM_DATES", "PROFILE_NEEDS_REVIEW"} or not project.topic_profile:
        profile = discover_project_profile(db, project)
    else:
        profile = {
            "status": project.status,
            "project_type": project.project_type,
            "topic_profile": project.topic_profile,
        }
    if project.project_type == "INSTITUTIONAL_PRODUCT" and project.status == "PROFILE_NEEDS_REVIEW":
        raise RuntimeError("Não foi possível confirmar o produto institucional; revise o perfil antes da coleta")
    stage("profile", "DONE", f"Perfil concluído: {project.project_type}")

    check()
    stage("search_plan", "RUNNING", "Gerando consultas determinísticas e consultas complementares com IA")
    planned = plan_queries_with_llm(db, project)
    stage("search_plan", "DONE", f"{len(planned)} consulta(s) criada(s)")

    check()
    stage("collection", "RUNNING", "Consultando fontes web")
    stage("youtube", "RUNNING", "Consultando YouTube quando a chave estiver configurada")
    collection_agents = collect_media_agents(
        db,
        project,
        cancel_check=check,
        web_progress=detail_for("collection"),
        youtube_progress=detail_for("youtube"),
    )
    collected = int(collection_agents["web"]["collected"])
    stage("collection", "DONE", f"{collected} novo(s) item(ns) coletado(s)")

    youtube = collection_agents["youtube"]
    youtube_collected = int(youtube["collected"])
    youtube_warning = None
    project.youtube_collection_status = str(youtube["status"])
    project.youtube_collection_error = youtube.get("error")
    if project.youtube_collection_status == "UNAVAILABLE":
        youtube_warning = "Coleta no YouTube indisponível; o relatório segue sem inferir ausência de cobertura."
        stage("youtube", "SKIPPED", youtube_warning)
    elif project.youtube_collection_status == "NOT_CONFIGURED":
        stage("youtube", "SKIPPED", "YOUTUBE_API_KEY não configurada")
    else:
        stage("youtube", "DONE", f"{youtube_collected} vídeo(s) coletado(s)")
    db.commit()

    check()
    stage("facts_pass_1", "RUNNING", "Extraindo pessoas, datas, causas, locais e evidências por campo")
    fact_pass_1 = extract_project_facts(
        db, project, cancel_check=check, progress_detail=detail_for("facts_pass_1")
    )
    stage("facts_pass_1", "DONE", f"{fact_pass_1['events_extracted']} evento(s) extraído(s)")

    check()
    stage("fact_resolution_1", "RUNNING", "Consolidando evidências e detectando conflitos entre fontes")
    fact_resolution_1 = resolve_project_facts(db, project)
    stage(
        "fact_resolution_1",
        "DONE",
        f"{fact_resolution_1['events']} evento(s); {fact_resolution_1['conflicts']} conflito(s)",
    )

    check()
    stage("nominal_plan", "RUNNING", "Criando buscas pelos nomes já identificados")
    nominal_created = plan_nominal_followups(db, project)
    stage("nominal_plan", "DONE", f"{nominal_created} consulta(s) nominal(is) criada(s)")

    second_collected = 0
    fact_pass_2 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_2 = fact_resolution_1
    if nominal_created:
        check()
        stage("nominal_collection", "RUNNING", "Buscando corroboradores e fontes complementares por nome")
        second_collected = collect_tavily(
            db, project.id, cancel_check=check, progress_detail=detail_for("nominal_collection")
        )
        stage("nominal_collection", "DONE", f"{second_collected} novo(s) item(ns) coletado(s)")

        check()
        stage("facts_pass_2", "RUNNING", "Extraindo evidências das novas fontes nominais")
        fact_pass_2 = extract_project_facts(
            db, project, cancel_check=check, progress_detail=detail_for("facts_pass_2")
        )
        stage("facts_pass_2", "DONE", f"{fact_pass_2['events_extracted']} evento(s) adicional(is) extraído(s)")

        check()
        stage("fact_resolution_2", "RUNNING", "Reconciliando a camada factual com as novas fontes")
        fact_resolution_2 = resolve_project_facts(db, project)
        stage(
            "fact_resolution_2",
            "DONE",
            f"{fact_resolution_2['events']} evento(s); {fact_resolution_2['conflicts']} conflito(s)",
        )
    else:
        stage("nominal_collection", "SKIPPED", "Nenhum nome novo exigiu segunda coleta")
        stage("facts_pass_2", "SKIPPED", "Nenhuma segunda coleta para extrair")
        stage("fact_resolution_2", "SKIPPED", "A consolidação da primeira passagem foi mantida")

    check()
    stage("validation", "RUNNING", "Validando janela de publicação e aderência temática")
    validation = validate_and_classify(
        db, project, cancel_check=check, progress_detail=detail_for("validation")
    )
    stage("validation", "DONE", f"{validation['valid']} válido(s); {validation['discarded']} descartado(s)")

    check()
    stage("classification", "RUNNING", "Classificando enquadramento, fidelidade e menção institucional")
    classification = classify_with_llm(
        db, project, cancel_check=check, progress_detail=detail_for("classification")
    )
    stage("classification", "DONE", f"{classification.get('updated', 0)} item(ns) classificado(s)")

    check()
    stage("report", "RUNNING", "Redigindo o relatório a partir do corpus e fatos já validados")
    drafted = draft_report_with_llm(db, project)
    stage("report", "DONE", "Relatório estruturado e persistido")

    check()
    stage("qa", "RUNNING", "Executando verificações determinísticas e auditoria final")
    qa = run_report_qa(db, project, drafted)
    stage("qa", "DONE", f"QA {qa['status']}")

    project.status = "REPORT_READY" if qa["approved"] else "REPORT_NEEDS_REVIEW"
    db.commit()

    return {
        "project": _project_payload(project),
        "profile": profile,
        "planned": len(planned),
        "collected": collected,
        "youtube_collected": youtube_collected,
        "nominal_queries_created": nominal_created,
        "second_pass_collected": second_collected,
        "fact_pass_1": fact_pass_1,
        "fact_resolution_1": fact_resolution_1,
        "fact_pass_2": fact_pass_2,
        "fact_resolution_2": fact_resolution_2,
        "validation": validation,
        "classification": classification,
        "qa": qa,
        **drafted,
    }

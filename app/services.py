from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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
from app.llm import llm_is_configured, structured_response, web_search_structured_response
from app.media_scout import MediaScout
from app.models import Classification, GeneratedReport, MediaItem, OfficialFact, Project, SearchQuery
from app.pdf_report import build_pdf
from app.prompts import ANALYST_PROMPT, DOCUMENTALIST_PROMPT, MEDIA_RELEVANCE_PROMPT, QUERY_PLANNER_PROMPT, WRITER_PROMPT
from app.report_qa import run_report_qa
from app.source_registry import (
    OFFICIAL_SECURITY_SOURCES,
    PRIORITY_MEDIA_SOURCES,
    PRIORITY_YOUTUBE_CHANNELS,
    PRIORITY_YOUTUBE_CHANNEL_ALIASES,
)
from app.topic_profile import build_topic_profile, normalized_terms, normalized_text, requested_month_window


PRIORITY_PORTALS = [*PRIORITY_MEDIA_SOURCES, ("YouTube", "youtube.com")]


EXECUTION_PROFILE_DEFAULTS = {
    "MIDIATICO_SIMPLES": {
        "enable_youtube": True,
        "enable_fact_layer": False,
        "enable_nominal_followup": False,
        "enable_cross_validation": True,
    },
    "MIDIATICO_COM_FATOS": {
        "enable_youtube": True,
        "enable_fact_layer": True,
        "enable_nominal_followup": False,
        "enable_cross_validation": True,
    },
    "COMPLETO_NOMINAL": {
        "enable_youtube": True,
        "enable_fact_layer": True,
        "enable_nominal_followup": True,
        "enable_cross_validation": True,
    },
}


def effective_execution_profile(project: Project) -> str:
    """Resolve AUTO somente depois que o perfil temático já está disponível."""
    selected = (project.execution_profile or "AUTO").upper()
    if selected in EXECUTION_PROFILE_DEFAULTS:
        return selected

    if project.project_type == "INSTITUTIONAL_PRODUCT":
        return "MIDIATICO_SIMPLES"

    if project.project_type == "EVENT_TOPIC":
        profile = project.topic_profile or {}
        event_type = str(profile.get("event_type") or "").upper()
        requested_fields = set(profile.get("requested_fact_fields") or [])
        nominal_event_types = {"DEATH", "HOMICIDE", "FEMICIDE", "FEMINICIDE", "MURDER"}
        if "subject_name" in requested_fields and event_type in nominal_event_types:
            return "COMPLETO_NOMINAL"
        return "MIDIATICO_COM_FATOS"

    return "MIDIATICO_SIMPLES"


def execution_flags(project: Project) -> tuple[str, dict[str, bool]]:
    profile = effective_execution_profile(project)
    flags = dict(EXECUTION_PROFILE_DEFAULTS[profile])
    overrides = project.execution_options or {}
    for key in flags:
        value = overrides.get(key)
        if isinstance(value, bool):
            flags[key] = value

    # Dependências lógicas: não existe busca nominal sem camada factual,
    # nem validação cruzada Tavily x YouTube sem coleta no YouTube.
    if not flags["enable_fact_layer"]:
        flags["enable_nominal_followup"] = False
    if not flags["enable_youtube"]:
        flags["enable_cross_validation"] = False
    return profile, flags


def is_youtube_host(host: str) -> bool:
    host = host.lower().split(":")[0]
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def is_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and is_youtube_host(parsed.netloc)


def normalized_channel_name(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", normalized_text(value or "")))


def matches_priority_youtube_channel(channel: str | None, label: str) -> bool:
    """Exige correspondência conservadora para uma checagem de canal-alvo."""
    source = normalized_channel_name(channel)
    if not source:
        return False
    canonical_name = next(
        (name for candidate, name in PRIORITY_YOUTUBE_CHANNELS if candidate == label),
        "",
    )
    aliases = [canonical_name, *(PRIORITY_YOUTUBE_CHANNEL_ALIASES.get(label) or [])]
    normalized_aliases = {normalized_channel_name(alias) for alias in aliases if alias}
    if source in normalized_aliases:
        return True
    return any(
        len(alias) >= 8 and (alias in source or source in alias)
        for alias in normalized_aliases
    )


def youtube_tasks_for_execution(project: Project) -> list:
    """Prioriza a auditoria de todos os canais antes das buscas temáticas.

    O teto configurável controla as buscas temáticas adicionais, mas nunca pode
    eliminar um canal prioritário da matriz de checagem.
    """
    settings = get_settings()
    tasks = MediaScout(project.topic, project.topic_profile).youtube_tasks()
    priority = [task for task in tasks if task.target != "Busca temática"]
    thematic = [task for task in tasks if task.target == "Busca temática"]
    task_budget = max(settings.max_youtube_tasks, len(priority))
    return [*priority, *thematic[: max(0, task_budget - len(priority))]]


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode({"v": video_id}), ""))
    query = ""
    if is_youtube_host(host) and parsed.path.rstrip("/") == "/watch":
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


def media_window(project: Project) -> tuple[date | None, date | None]:
    """Retorna a janela midiática somente quando ela foi realmente definida.

    Quando o usuário não informou datas e o tema também não permitiu inferir
    uma janela temporal, a coleta deve ser temática. Nesse caso devolvemos
    ``(None, None)`` para que os coletores não recebam um filtro artificial de
    um único dia.
    """
    if not project.has_custom_date_window:
        return None, None
    return project.collection_start, project.collection_end


def query_window(project: Project, query: SearchQuery) -> tuple[date | None, date | None]:
    """Resolve a janela de busca sem inventar datas.

    - OFFICIAL_FACT: sempre temática/documental, sem filtro temporal obrigatório.
    - Projetos sem janela explícita/inferida: busca temática, sem start/end.
    - FACT_DISCOVERY/NOMINAL_FOLLOWUP com janela: usa a janela factual + grace days.
    - MEDIA_REPERCUSSION com janela: usa a janela de repercussão.
    """
    if query.purpose == "OFFICIAL_FACT":
        return None, None

    if not project.has_custom_date_window:
        return None, None

    if query.purpose in {"FACT_DISCOVERY", "NOMINAL_FOLLOWUP"}:
        start = project.event_start or project.collection_start
        end = project.event_end or project.collection_end
        return start, end + timedelta(days=project.fact_grace_days or 0)

    return project.collection_start, project.collection_end


def _valid_search_window(start: date | None, end: date | None) -> bool:
    """APIs como Tavily rejeitam ``start_date == end_date``.

    Só consideramos uma janela apta a ser enviada ao provedor quando existem
    duas datas e o início é estritamente anterior ao fim.
    """
    return bool(start and end and start < end)


PROJECT_TYPE_LABELS = {
    "INSTITUTIONAL_PRODUCT": "Produto institucional",
    "EVENT_TOPIC": "Tema factual / evento",
    "GENERAL_TOPIC": "Tema geral",
    "AUTO": "Classificação automática",
}


def _trusted_launch_date(project: Project) -> date | None:
    options = project.execution_options or {}
    profile = project.topic_profile or {}
    trusted = bool(options.get("launch_date_user_supplied") or profile.get("launch_date_confirmed"))
    return project.launch_date if trusted else None


def _project_payload(project: Project, *, for_report: bool = False) -> dict:
    """Serializa o projeto sem transformar datas técnicas em fatos editoriais.

    Durante busca/planejamento, uma pauta TOPIC_DRIVEN não recebe janela artificial.
    Durante redação/exportação, a janela observada é exibida somente quando veio
    do usuário/tema ou foi realmente observada no corpus validado.
    """
    execution_profile, flags = execution_flags(project)
    has_explicit_window = bool(project.has_custom_date_window)
    profile = project.topic_profile or {}

    observed_start = profile.get("observed_collection_start") if for_report else None
    observed_end = profile.get("observed_collection_end") if for_report else None
    collection_start = (
        project.collection_start.isoformat()
        if has_explicit_window and project.collection_start
        else observed_start
    )
    collection_end = (
        project.collection_end.isoformat()
        if has_explicit_window and project.collection_end
        else observed_end
    )
    trusted_launch = _trusted_launch_date(project)

    return {
        "id": project.id,
        "topic": project.topic,
        "institution": project.institution,
        "project_type": project.project_type,
        "project_type_label": PROJECT_TYPE_LABELS.get(project.project_type, project.project_type),
        "execution_profile": project.execution_profile or "AUTO",
        "effective_execution_profile": execution_profile,
        "execution_options": project.execution_options or {},
        "execution_flags": flags,
        "temporal_mode": "EXPLICIT_WINDOW" if has_explicit_window else "TOPIC_DRIVEN",
        "launch_date": trusted_launch.isoformat() if trusted_launch else None,
        "collection_start": collection_start,
        "collection_end": collection_end,
        "search_start": project.collection_start.isoformat() if has_explicit_window and project.collection_start else None,
        "search_end": project.collection_end.isoformat() if has_explicit_window and project.collection_end else None,
        "event_start": project.event_start.isoformat() if project.event_start else None,
        "event_end": project.event_end.isoformat() if project.event_end else None,
        "fact_grace_days": project.fact_grace_days,
    }


def discover_project_profile(db: Session, project: Project) -> dict:
    """Descobre e confirma um produto institucional sem confundir existência e data de lançamento.

    Estados distintos ficam registrados em ``topic_profile``:
    - product_status=PUBLISHED: há evidência de que o produto já foi publicado/divulgado;
    - product_status=ANNOUNCED: o produto foi anunciado, mas a publicação efetiva não foi confirmada;
    - product_status=NOT_CONFIRMED: as fontes coletadas não confirmam o produto;
    - launch_date_confirmed=True: existe evidência explícita da data REAL de lançamento.

    Uma previsão de lançamento nunca vira ``launch_date`` confirmado.
    """
    profile = build_topic_profile(project.topic)
    project.project_type = profile["project_type"]
    project.topic_profile = profile

    # Só propagamos datas da repercussão para os fatos quando existe uma
    # janela explícita/inferida. Sem datas, a pauta permanece temática.
    if project.has_custom_date_window:
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

    settings = get_settings()
    tavily_key = settings.tavily_api_key
    product_name = str(profile.get("product_name") or project.topic or "").strip()

    # A descoberta de produto usa fontes oficiais atuais do registry, em vez de
    # depender apenas do domínio legado isp.rj.gov.br.
    preferred_official_labels = {"ISP", "ISP Conecta", "Governo do RJ"}
    official_domains = [
        source["domain"]
        for source in OFFICIAL_SECURITY_SOURCES
        if source.get("label") in preferred_official_labels
    ]

    # O orçamento cobre TODAS as chamadas externas da descoberta do produto:
    # 1 perfil do tema (build_topic_profile) + 1 agente documentalista +
    # o restante disponível para as buscas. Assim a etapa nunca passa de
    # max_profile_discovery_calls chamadas mesmo quando Tavily está ausente.
    call_budget = max(1, min(4, settings.max_profile_discovery_calls))
    llm_available = llm_is_configured()
    profile_reserved = 1 if llm_available else 0
    analysis_reserved = 1 if llm_available else 0
    search_budget = max(0, call_budget - profile_reserved - analysis_reserved)
    searches = [
        f'"{product_name}" "Instituto de Segurança Pública"',
        f'"{product_name}" divulgado',
        *[f'site:{domain} "{product_name}"' for domain in official_domains],
    ]
    searches = list(dict.fromkeys(query for query in searches if query.strip()))[:search_budget]

    sources: list[dict] = []
    seen: set[str] = set()

    # O primeiro request real funciona como teste do Tavily. Se houver erro
    # duro de plano/quota/autenticação, o restante da descoberta vai direto
    # para OpenAI Web Search, sem insistir no Tavily nesta execução.
    tavily_client = None
    if tavily_key:
        try:
            from tavily import TavilyClient
            tavily_client = TavilyClient(api_key=tavily_key)
        except Exception:
            tavily_client = None

    tavily_circuit_open = tavily_client is None
    tavily_disable_reason = "TAVILY_API_KEY não configurada" if tavily_client is None else None
    discovery_stats = {
        "call_budget": call_budget,
        "profile_reserved": profile_reserved,
        "analysis_reserved": analysis_reserved,
        "search_budget": search_budget,
        "external_search_calls": 0,
        "profile_analysis_calls": 0,
        "tavily_attempts": 0,
        "tavily_successes": 0,
        "tavily_circuit_open": bool(tavily_circuit_open),
        "web_search_queries": 0,
    }

    fallback_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "results": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "content": {"type": ["string", "null"]},
                    },
                    "required": ["title", "url", "content"],
                },
            }
        },
        "required": ["results"],
    }

    for query in searches:
        # Uma consulta equivale a UMA chamada externa. O fallback é usado nas
        # próximas consultas quando Tavily fica indisponível, nunca para repetir
        # a mesma intenção apenas porque ela não retornou itens.
        if discovery_stats["external_search_calls"] >= search_budget:
            break

        if not tavily_circuit_open and tavily_client is not None:
            discovery_stats["external_search_calls"] += 1
            discovery_stats["tavily_attempts"] += 1
            try:
                response = tavily_client.search(
                    query=query,
                    max_results=5,
                    include_raw_content="text",
                    search_depth="advanced",
                    timeout=15,
                )
                discovery_stats["tavily_successes"] += 1
                for result in response.get("results", []):
                    url = (result.get("url") or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append(
                        {
                            "title": result.get("title", "Sem título"),
                            "url": url,
                            "content": (result.get("raw_content") or result.get("content") or "")[:3500],
                            "search_provider": "tavily",
                            "search_query": query,
                        }
                    )
            except Exception as exc:
                if _is_tavily_hard_failure(exc):
                    tavily_circuit_open = True
                    tavily_disable_reason = str(exc)[:1000]
                    discovery_stats["tavily_circuit_open"] = True
        elif llm_is_configured():
            site_match = re.search(r"(?:^|\s)site:([^\s]+)", query, flags=re.IGNORECASE)
            site_domain = site_match.group(1).strip().strip('"\'()[]{}').split('/')[0] if site_match else None
            allowed_domains = [site_domain] if site_domain else None

            try:
                discovery_stats["external_search_calls"] += 1
                discovery_stats["web_search_queries"] += 1
                fallback = web_search_structured_response(
                    instructions=(
                        "Pesquise fontes REAIS para confirmar a existência e, separadamente, a publicação de um "
                        "produto institucional do Instituto de Segurança Pública. Preserve o nome exato do produto. "
                        "Priorize páginas oficiais do ISP/Governo do RJ e fontes jornalísticas confiáveis. "
                        "Uma previsão futura de lançamento não prova publicação efetiva. Não invente datas, URLs ou "
                        "conteúdo. Retorne somente resultados materialmente relacionados ao produto pesquisado."
                    ),
                    payload={
                        "query": query,
                        "topic": project.topic,
                        "product_name": product_name,
                        "tavily_unavailable_reason": tavily_disable_reason,
                    },
                    schema_name="institutional_profile_web_sources_v2",
                    schema=fallback_schema,
                    allowed_domains=allowed_domains,
                    model=settings.web_search_model,
                    retry_without_domain_filter=False,
                    max_output_tokens=3500,
                )
                for result in fallback.get("results", []):
                    url = (result.get("url") or "").strip()
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append(
                        {
                            "title": result.get("title") or "Sem título",
                            "url": url,
                            "content": (result.get("content") or "")[:3500],
                            "search_provider": "openai_web_search",
                            "search_query": query,
                        }
                    )
            except RuntimeError:
                pass

        if len(sources) >= 10:
            break

    sources = sources[:10]
    if not sources or not llm_is_configured():
        profile_state = dict(project.topic_profile or {})
        profile_state.update(
            {
                "product_status": "NOT_CONFIRMED",
                "product_confirmed": False,
                "product_published": False,
                "launch_date_confirmed": False,
                "launch_status": "NOT_FOUND",
                "profile_discovery_stats": discovery_stats,
            }
        )
        project.topic_profile = profile_state
        project.status = "PROFILE_NEEDS_REVIEW"
        db.commit()
        return {
            "status": project.status,
            "project_type": project.project_type,
            "sources": len(sources),
            "facts": 0,
            "product_confirmed": False,
            "product_published": False,
            "launch_date_confirmed": False,
            "launch_required": False,
            "warning": "Não foi possível confirmar documentalmente a publicação do produto",
            "discovery_stats": discovery_stats,
        }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "institution": {"type": ["string", "null"]},
            "product_status": {
                "type": "string",
                "enum": ["PUBLISHED", "ANNOUNCED", "NOT_CONFIRMED"],
            },
            "product_evidence": {"type": ["string", "null"]},
            "product_source_index": {"type": ["integer", "null"], "minimum": 0},
            "launch_status": {
                "type": "string",
                "enum": ["CONFIRMED_ACTUAL", "EXPECTED_ONLY", "NOT_FOUND"],
            },
            "launch_date": {"type": ["string", "null"]},
            "expected_launch_date": {"type": ["string", "null"]},
            "launch_evidence": {"type": ["string", "null"]},
            "launch_source_index": {"type": ["integer", "null"], "minimum": 0},
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
        "required": [
            "institution",
            "product_status",
            "product_evidence",
            "product_source_index",
            "launch_status",
            "launch_date",
            "expected_launch_date",
            "launch_evidence",
            "launch_source_index",
            "official_facts",
        ],
    }

    discovery_stats["profile_analysis_calls"] = 1
    result = structured_response(
        instructions=DOCUMENTALIST_PROMPT,
        payload={"topic": project.topic, "product_name": product_name, "sources": sources},
        schema_name="institutional_product_profile_v2",
        schema=schema,
        max_output_tokens=5500,
    )

    if result.get("institution"):
        project.institution = str(result["institution"])[:200]

    product_status = str(result.get("product_status") or "NOT_CONFIRMED")
    launch_status = str(result.get("launch_status") or "NOT_FOUND")

    def source_from_index(value: object) -> dict | None:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return sources[value] if 0 <= value < len(sources) else None

    product_source = source_from_index(result.get("product_source_index"))
    launch_source = source_from_index(result.get("launch_source_index"))

    launch_confirmed = False
    parsed_launch_date: date | None = None
    if launch_status == "CONFIRMED_ACTUAL" and result.get("launch_date"):
        try:
            parsed_launch_date = date.fromisoformat(str(result["launch_date"])[:10])
            launch_confirmed = bool(result.get("launch_evidence"))
        except ValueError:
            parsed_launch_date = None
            launch_confirmed = False

    # Uma data real de lançamento confirmada implica que o produto já foi publicado.
    if launch_confirmed:
        product_status = "PUBLISHED"

    product_confirmed = product_status in {"PUBLISHED", "ANNOUNCED"}
    product_published = product_status == "PUBLISHED"

    # Se o usuário informou manualmente a data, não a sobrescrevemos. Caso contrário,
    # somente uma data REAL confirmada documentalmente pode atualizar launch_date.
    options = project.execution_options or {}
    if launch_confirmed and parsed_launch_date and not options.get("launch_date_user_supplied"):
        project.launch_date = parsed_launch_date

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

    expected_launch_date = None
    if result.get("expected_launch_date"):
        try:
            expected_launch_date = date.fromisoformat(str(result["expected_launch_date"])[:10]).isoformat()
        except ValueError:
            expected_launch_date = None

    profile_state = dict(project.topic_profile or {})
    profile_state.update(
        {
            "product_status": product_status,
            "product_confirmed": product_confirmed,
            "product_published": product_published,
            "product_confirmation_evidence": result.get("product_evidence"),
            "product_confirmation_source": product_source.get("url") if product_source else None,
            "launch_status": launch_status,
            "launch_date_confirmed": bool(launch_confirmed),
            "launch_evidence": result.get("launch_evidence"),
            "launch_confirmation_source": launch_source.get("url") if launch_source else None,
            "expected_launch_date": expected_launch_date,
            "profile_discovery_stats": discovery_stats,
        }
    )
    project.topic_profile = profile_state

    # A data exata deixa de ser requisito para aceitar o perfil. Se há evidência
    # de que o produto já foi publicado, o perfil está resolvido mesmo sem data.
    project.status = "PROFILED" if product_published else "PROFILE_NEEDS_REVIEW"
    db.commit()

    warning = None
    if product_published and not launch_confirmed:
        warning = "Produto publicado confirmado; data exata de lançamento não localizada"
    elif product_status == "ANNOUNCED":
        warning = "Produto anunciado, mas publicação efetiva ainda não confirmada"
    elif not product_confirmed:
        warning = "Produto institucional identificado pelo tema, mas não confirmado documentalmente nas fontes coletadas"

    return {
        "status": project.status,
        "project_type": project.project_type,
        "sources": len(sources),
        "facts": facts_added,
        "product_status": product_status,
        "product_confirmed": product_confirmed,
        "product_published": product_published,
        "launch_status": launch_status,
        "launch_date_confirmed": launch_confirmed,
        "launch_date": parsed_launch_date.isoformat() if launch_confirmed and parsed_launch_date else None,
        "expected_launch_date": expected_launch_date,
        "launch_required": False,
        "warning": warning,
        "discovery_stats": discovery_stats,
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
    # Hard guard central: consultas de repercussão/fato precisam preservar
    # a identidade do objeto monitorado. Produto institucional usa âncora
    # nominal; EVENT_TOPIC conhecido usa âncora factual + local/período.
    if not _query_preserves_project_anchor(project, query, purpose=purpose):
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

    # Eventos com âncora factual usam variantes controladas. Isso evita gerar
    # combinações vagas como "morte Rio" ou "polícia 2026".
    if project.project_type == "EVENT_TOPIC" and (
        profile.get("event_anchor") or profile.get("fact_discovery_variants")
    ):
        variants = [
            str(value).strip()
            for value in (
                profile.get("fact_discovery_variants")
                or profile.get("event_search_variants")
                or [profile.get("event_anchor")]
            )
            if str(value or "").strip()
        ]
        locations = [str(value).strip() for value in (profile.get("locations") or []) if str(value).strip()]
        location = next((value for value in locations if len(value) > 2), "")
        year = ""
        if project.event_start and project.event_end and project.event_start.year == project.event_end.year:
            year = str(project.event_start.year)
        else:
            match = re.search(r"(?<!\d)(20\d{2})(?!\d)", project.topic or "")
            year = match.group(1) if match else ""

        bases: list[str] = []
        for variant in variants[:8]:
            query = f'"{variant}"'
            if location and normalized_text(location) not in normalized_text(variant):
                query += f' "{location}"'
            if year and year not in variant:
                query += f" {year}"
            bases.append(query.strip())
        return list(dict.fromkeys(bases))[:8]

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


def _institutional_product_profile(project: Project) -> tuple[str, str, list[str], list[str]]:
    """Retorna nome, ancora, variantes de busca e termos de assunto do produto.

    A separacao e proposital: variantes do produto podem orientar a descoberta;
    termos de assunto servem apenas para confirmar atribuicao/relevancia e nunca
    devem virar buscas autonomas de repercussao.
    """
    profile = project.topic_profile or {}
    product_name = " ".join(str(profile.get("product_name") or project.topic or "").split()).strip()
    product_anchor = " ".join(str(profile.get("product_anchor") or "").split()).strip()
    if not product_anchor:
        product_anchor = re.sub(r"\b20\d{2}\b", "", product_name).strip()
        product_anchor = " ".join(product_anchor.split()) or product_name

    variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("product_search_variants") or [])
        if str(value).strip()
    ]
    if not variants:
        variants = [product_name, product_anchor]
    variants = list(dict.fromkeys(value for value in variants if value))

    subject_terms = [
        normalized_text(str(value)).strip()
        for value in (profile.get("subject_terms") or [])
        if str(value).strip()
    ]
    if not subject_terms:
        generic = {
            "dossie", "relatorio", "boletim", "anuario", "estudo", "publicacao",
            "pesquisa", "documento", "edicao", "serie", "instituto", "seguranca",
            "publica", "rio", "janeiro",
        }
        subject_terms = sorted(
            term for term in normalized_terms(product_name) if term not in generic
        )
    return product_name, product_anchor, variants, subject_terms


def _institutional_query_anchor(project: Project) -> str:
    """Ancora nominal minima que toda busca de produto deve preservar."""
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return ""
    _, anchor, _, _ = _institutional_product_profile(project)
    return anchor


def _query_preserves_institutional_anchor(project: Project, query_text: str) -> bool:
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True

    _, anchor, variants, _ = _institutional_product_profile(project)
    # Remove apenas o operador site:, preservando o restante da consulta.
    query_norm = normalized_text(re.sub(r"(?:^|\s)site:[^\s]+", " ", query_text or ""))
    query_norm = " ".join(query_norm.split())
    acceptable = [anchor, *variants]
    for candidate in acceptable:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in query_norm:
            return True
    return False


def _event_topic_profile(project: Project) -> tuple[str, list[str], list[str], list[str], str]:
    profile = project.topic_profile or {}
    anchor = " ".join(str(profile.get("event_anchor") or "").split()).strip()
    variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("event_search_variants") or [])
        if str(value).strip()
    ]
    fact_variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("fact_discovery_variants") or [])
        if str(value).strip()
    ]
    locations = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("locations") or [])
        if str(value).strip()
    ]
    year = ""
    if project.event_start and project.event_end and project.event_start.year == project.event_end.year:
        year = str(project.event_start.year)
    else:
        match = re.search(r"(?<!\d)(20\d{2})(?!\d)", project.topic or "")
        year = match.group(1) if match else ""
    return anchor, variants, fact_variants, locations, year


def _query_preserves_event_anchor(project: Project, query_text: str, *, purpose: str) -> bool:
    if project.project_type != "EVENT_TOPIC":
        return True

    anchor, variants, fact_variants, locations, year = _event_topic_profile(project)
    if not anchor and not variants:
        return True

    query_norm = normalized_text(re.sub(r"(?:^|\s)site:[^\s]+", " ", query_text or ""))
    query_norm = " ".join(query_norm.split())
    acceptable = [anchor, *variants]
    if purpose == "FACT_DISCOVERY":
        acceptable.extend(fact_variants)

    semantic_hit = any(
        (candidate_norm := " ".join(normalized_text(candidate).split()))
        and candidate_norm in query_norm
        for candidate in acceptable
    )
    if not semantic_hit:
        return False

    # Se o pedido trouxe território, ele precisa continuar presente na consulta.
    if locations:
        location_hit = False
        query_tokens = set(re.findall(r"[a-z0-9]+", query_norm))
        for location in locations:
            location_norm = " ".join(normalized_text(location).split())
            if location_norm and location_norm in query_norm:
                location_hit = True
                break
            if location_norm == "rj" and "rj" in query_tokens:
                location_hit = True
                break
        if not location_hit:
            return False

    # Ano explícito/inferido também é preservado. O filtro de data do provedor
    # continua existindo, mas a consulta textual fica auditável e específica.
    if year and year not in query_norm:
        return False
    return True


def _query_preserves_project_anchor(project: Project, query_text: str, *, purpose: str) -> bool:
    if purpose not in {"MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"}:
        return True
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        return purpose != "MEDIA_REPERCUSSION" or _query_preserves_institutional_anchor(project, query_text)
    if project.project_type == "EVENT_TOPIC":
        return _query_preserves_event_anchor(project, query_text, purpose=purpose)
    return True


def _institutional_product_version_guard_text(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> tuple[bool, str | None]:
    """Bloqueia edicoes divergentes do mesmo produto institucional.

    Exemplo: se o objeto for ``Dossie Mulher 2026``, um item que trate
    explicitamente apenas de ``Dossie Mulher 2025`` e rejeitado antes de ser
    persistido ou enviado ao LLM. Uma comparacao 2025 x 2026 continua
    permitida quando a edicao-alvo tambem aparece materialmente no texto.
    """
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True, None

    product_name, product_anchor, _, _ = _institutional_product_profile(project)
    requested_years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", product_name or "")
    if not requested_years:
        requested_years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", project.topic or "")
    if not requested_years:
        return True, None

    requested_year = requested_years[0]
    product_base = " ".join(normalized_text(product_anchor or product_name).split()).strip()
    if len(product_base) < 5:
        return True, None

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return True, None

    # Captura somente anos associados nominalmente ao mesmo produto. Assim,
    # um simples ano de publicacao no restante da pagina nao vira evidencia
    # de que a edicao correta foi mencionada.
    pattern = rf"{re.escape(product_base)}\s+(20\d{{2}})"
    mentioned_editions = set(re.findall(pattern, haystack))

    if mentioned_editions and requested_year not in mentioned_editions:
        editions = ", ".join(sorted(mentioned_editions))
        return (
            False,
            f"Edicao incompatível com o produto solicitado: edicao-alvo {requested_year}; "
            f"edicao(oes) encontrada(s): {editions}.",
        )

    return True, None


def _institutional_collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    """Filtro deterministico barato antes de persistir ou enviar o item ao LLM.

    Aceita imediatamente quando o nome/ancora do produto aparece. Quando isso
    nao ocorre, permite apenas atribuicao plausivel: ISP/instituicao + termo de
    assunto do produto + linguagem de atribuicao/estudo. Assim, um resultado
    como "Dossie Master" e rejeitado antes de consumir validacao semantica,
    enquanto uma materia cujo titulo nao cite o produto ainda pode seguir se o
    corpo disser, por exemplo, "segundo levantamento do ISP...".
    """
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True

    version_ok, _ = _institutional_product_version_guard_text(
        project,
        title=title,
        snippet=snippet,
        content=content,
    )
    if not version_ok:
        return False

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return False

    product_name, anchor, variants, subject_terms = _institutional_product_profile(project)
    nominal_candidates = [product_name, anchor, *variants]
    for candidate in nominal_candidates:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in haystack:
            return True

    institution_terms = {
        normalized_text(project.institution or ""),
        "instituto de seguranca publica",
        "isp rj",
    }
    profile = project.topic_profile or {}
    institution_terms.update(
        normalized_text(str(value))
        for value in (profile.get("organizations") or [])
        if str(value).strip()
    )
    institution_hit = any(
        term and len(term) >= 5 and term in haystack
        for term in institution_terms
    ) or bool(re.search(r"\bisp(?:-rj)?\b", haystack))

    subject_hit = any(
        term and len(term) >= 4 and term in haystack
        for term in subject_terms
    )

    body_terms = normalized_terms(body)
    attribution_words = {
        "dossie", "relatorio", "boletim", "estudo", "levantamento", "pesquisa",
        "dados", "aponta", "apontou", "segundo", "conforme", "divulgado", "divulgou",
    }
    attribution_hit = bool(body_terms.intersection(attribution_words))

    return bool(institution_hit and subject_hit and attribution_hit)


def _event_collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    """Pré-filtro barato para EVENT_TOPIC com âncora factual conhecida.

    Não exige a redação oficial literal: aceita também formulações jornalísticas
    equivalentes, mas bloqueia resultados que só compartilham palavras amplas
    como "morte", "polícia" ou "Rio".
    """
    if project.project_type != "EVENT_TOPIC":
        return True

    anchor, variants, fact_variants, _, _ = _event_topic_profile(project)
    if not anchor and not variants:
        return True

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return False

    for candidate in [anchor, *variants, *fact_variants]:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in haystack:
            return True

    # Fallback semântico determinístico para MIAE e variantes jornalísticas.
    profile = project.topic_profile or {}
    event_type = str(profile.get("event_type") or "").upper()
    if event_type == "DEATH_BY_STATE_INTERVENTION":
        death_hit = any(
            term in haystack
            for term in ("morte", "mortes", "morto", "morta", "morre", "morreu", "obito", "letalidade")
        )
        police_action_hit = any(
            term in haystack
            for term in (
                "intervencao policial",
                "intervencao de agente do estado",
                "intervencao de agentes do estado",
                "acao policial",
                "operacao policial",
                "morto por policial",
                "morta por policial",
                "baleado por policial",
                "baleada por policial",
                "policia matou",
                "letalidade policial",
            )
        )
        return bool(death_hit and police_action_hit)

    return False


def _collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    if not _institutional_collection_guard(project, title=title, snippet=snippet, content=content):
        return False
    if not _event_collection_guard(project, title=title, snippet=snippet, content=content):
        return False
    return True


def plan_queries(db: Session, project: Project) -> list[SearchQuery]:
    if not project.topic_profile:
        project.topic_profile = build_topic_profile(project.topic)
        project.project_type = project.topic_profile["project_type"]

    settings = get_settings()
    query_limit = max(1, settings.max_search_queries)
    existing = _existing_queries(db, project.id)
    created: list[SearchQuery] = []
    _, flags = execution_flags(project)

    scout_tasks = MediaScout(project.topic, project.topic_profile).web_tasks()
    thematic_tasks = [task for task in scout_tasks if task.target == "Busca temática"]
    portal_tasks = [task for task in scout_tasks if task.target != "Busca temática"]

    def add_media_task(task) -> None:
        if len(created) >= query_limit:
            return
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

    # Em EVENT_TOPIC com camada factual, reservamos orçamento desde o início
    # para descoberta de casos e fontes oficiais. Sem isso, as checagens de
    # portais podem consumir MAX_SEARCH_QUERIES inteiro antes da camada factual.
    if project.project_type == "EVENT_TOPIC" and flags["enable_fact_layer"]:
        for task in thematic_tasks[:2]:
            add_media_task(task)

        bases = _fact_query_bases(project)
        fact_budget = min(4, max(0, query_limit - len(created)))
        fact_added = 0
        for base in bases:
            if len(created) >= query_limit or fact_added >= fact_budget:
                break
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
                fact_added += 1

        compact = bases[0] if bases else project.topic
        official_budget = min(3, max(0, query_limit - len(created)))
        official_added = 0
        for source in OFFICIAL_SECURITY_SOURCES:
            if len(created) >= query_limit or official_added >= official_budget:
                break
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
                official_added += 1

        # O restante do orçamento é usado para checagens nominais de portais.
        for task in portal_tasks:
            if len(created) >= query_limit:
                break
            add_media_task(task)

        # Se houve duplicatas ou alguma consulta foi rejeitada pelo hard guard,
        # aproveitamos vagas remanescentes com variantes temáticas estritas.
        for task in thematic_tasks[2:]:
            if len(created) >= query_limit:
                break
            add_media_task(task)
    else:
        # Perfis exclusivamente midiáticos preservam o comportamento anterior:
        # busca temática principal, portais prioritários e variantes restantes.
        ordered_tasks = [
            *thematic_tasks[:1],
            *portal_tasks,
            *thematic_tasks[1:],
        ]
        for task in ordered_tasks:
            if len(created) >= query_limit:
                break
            add_media_task(task)

    db.commit()
    return created


def plan_queries_with_llm(db: Session, project: Project) -> list[SearchQuery]:
    settings = get_settings()
    deterministic = plan_queries(db, project)
    if not llm_is_configured() or len(deterministic) >= settings.max_search_queries:
        return deterministic

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "queries": {
                "type": "array",
                "maxItems": max(1, settings.max_llm_search_queries),
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
    _, flags = execution_flags(project)
    allowed_purposes = ["MEDIA_REPERCUSSION"]
    if project.project_type == "EVENT_TOPIC" and flags["enable_fact_layer"]:
        allowed_purposes.extend(["FACT_DISCOVERY", "OFFICIAL_FACT"])

    result = structured_response(
        instructions=(
            QUERY_PLANNER_PROMPT
            + " Respeite rigorosamente allowed_purposes; não proponha consultas de finalidade desativada. "
            + "Quando project.temporal_mode for TOPIC_DRIVEN, NÃO invente datas nem force mês/período. "
            + "Planeje consultas a partir do significado da pauta: nome exato do tema, instituição, atores, "
            + "ações, locais, sinônimos e combinações úteis para descobrir a repercussão real. "
            + "Para produtos institucionais, TODA consulta MEDIA_REPERCUSSION deve preservar a ancora nominal "
            + "do produto (por exemplo, 'Dossie Mulher'), preferencialmente entre aspas. Nunca gere consultas "
            + "com apenas termos genericos como 'dossie', 'mulher', 'relatorio' ou o tema amplo isolado. "
            + "Para EVENT_TOPIC com event_anchor, preserve a categoria factual completa. MEDIA_REPERCUSSION e "
            + "OFFICIAL_FACT devem usar event_anchor/event_search_variants; FACT_DISCOVERY pode usar também "
            + "fact_discovery_variants. Preserve local e ano explícitos e não gere buscas vagas como 'morte Rio' "
            + "ou 'policia 2026'. Quando houver janela explícita, use-a apenas como contexto temporal."
        ),
        payload={
            "project": _project_payload(project),
            "topic_profile": project.topic_profile,
            "allowed_purposes": allowed_purposes,
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
        if len(created) >= settings.max_search_queries:
            break
        if item["purpose"] not in allowed_purposes:
            continue
        if not _query_preserves_project_anchor(
            project, item["query"], purpose=item["purpose"]
        ):
            continue
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


def _record_source_provenance(
    item: MediaItem,
    *,
    source: str,
    title: str | None,
    url: str,
    published_at: object = None,
    snippet: str | None = None,
    channel: str | None = None,
    view_count: int | None = None,
    query: str | None = None,
    target: str | None = None,
) -> None:
    """Acrescenta evidência por coleta, sem apagar o histórico auditável."""
    snapshots = list(item.source_provenance or [])
    snapshot = {
        "source": source,
        "title": title,
        "url": url,
        "published_at": str(published_at) if published_at else None,
        "snippet": snippet,
        "channel": channel,
        "view_count": view_count,
        "query": query,
        "target": target,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    item.source_provenance = [*snapshots, snapshot]


def _site_domain_from_query(query_text: str) -> str | None:
    """Extrai ``site:dominio`` para reaproveitar a intenção no Web Search."""
    match = re.search(r"(?:^|\s)site:([^\s]+)", query_text or "", flags=re.IGNORECASE)
    if not match:
        return None
    domain = match.group(1).strip().strip('"\'()[]{}').lower()
    domain = domain.split("/")[0].split(":")[0]
    return domain or None


def _collect_openai_web_search_query(
    db: Session,
    project: Project,
    query: SearchQuery,
    existing_items: dict[str, MediaItem],
    *,
    max_results: int,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Executa uma consulta via OpenAI Web Search como fallback do Tavily.

    O schema foi mantido deliberadamente simples. O Web Search cuida de
    localizar paginas; a relevancia fina continua sendo decidida pelo hard
    guard deterministico e pela validacao semantica em lote. Isso torna o
    fallback mais robusto do que pedir que a mesma chamada pesquise e faca
    classificacao editorial detalhada ao mesmo tempo.
    """
    if not llm_is_configured():
        raise RuntimeError("OPENAI_API_KEY não configurada para fallback Web Search")

    if cancel_check:
        cancel_check()

    settings = get_settings()
    start, end = query_window(project, query)
    has_window = _valid_search_window(start, end)
    site_domain = _site_domain_from_query(query.query)
    allowed_domains = [site_domain] if site_domain else None

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "results": {
                "type": "array",
                "maxItems": max(1, min(20, max_results)),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "published_at": {"type": ["string", "null"]},
                        "snippet": {"type": ["string", "null"]},
                        "source_name": {"type": ["string", "null"]},
                        "evidence": {"type": ["string", "null"]},
                    },
                    "required": [
                        "title", "url", "published_at", "snippet", "source_name", "evidence",
                    ],
                },
            }
        },
        "required": ["results"],
    }

    instructions = """Você é o agente de fallback de pesquisa web de um sistema de repercussão midiática.
Use obrigatoriamente a ferramenta de pesquisa web e execute a consulta recebida. Retorne somente
paginas REAIS e acessiveis. Nao invente URL, titulo, data, veiculo ou trecho.

Quando houver um operador site: na consulta, priorize esse dominio. Se o filtro tecnico de dominio
nao estiver disponivel, preserve o operador site: que ja existe no texto da consulta.

Para produto institucional, mantenha o nome/ancora do produto como foco da busca. Uma pagina que
apenas trate do mesmo assunto amplo nao deve ser retornada como repercussao do produto. O campo
`evidence` deve trazer, quando disponivel, um trecho curto que mostre a ligacao com o produto,
instituicao ou dado atribuido. Se nao houver trecho verificavel, use null.

Quando temporal_mode=explicit_window, priorize a janela informada. Quando for topic_driven, nao
invente datas. Uma lista vazia e preferivel a resultados duvidosos. Retorne apenas o JSON pedido."""

    if progress_detail:
        provider_note = f" restrito a {site_domain}" if site_domain else ""
        progress_detail(f"OpenAI Web Search{provider_note}: {query.query[:100]}")

    result = web_search_structured_response(
        instructions=instructions,
        payload={
            "project": _project_payload(project),
            "topic_profile": project.topic_profile or {},
            "query": query.query,
            "purpose": query.purpose,
            "rationale": query.rationale,
            "collection_start": start.isoformat() if has_window and start else None,
            "collection_end": end.isoformat() if has_window and end else None,
            "temporal_mode": "explicit_window" if has_window else "topic_driven",
        },
        schema_name="web_search_fallback_results_v3",
        schema=schema,
        allowed_domains=allowed_domains,
        max_output_tokens=4200,
        model=settings.web_search_model,
        retry_without_domain_filter=True,
        fallback_model=settings.youtube_search_model,
    )

    returned = 0
    added = 0
    rejected = 0
    duplicate_hits = 0

    for item_data in (result.get("results") or [])[:max_results]:
        returned += 1
        url = (item_data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            rejected += 1
            continue

        snippet = item_data.get("snippet")
        evidence = item_data.get("evidence")
        guard_content = " ".join(
            part for part in [snippet, evidence] if isinstance(part, str) and part.strip()
        )
        if query.purpose == "MEDIA_REPERCUSSION" and not _collection_guard(
            project,
            title=item_data.get("title"),
            snippet=snippet,
            content=guard_content,
        ):
            rejected += 1
            continue

        canonical = canonicalize(url)
        host = urlparse(url).netloc.lower()
        published_at = result_publication_date(item_data.get("published_at"))
        source_name = item_data.get("source_name") or host
        existing = existing_items.get(canonical)

        if existing:
            duplicate_hits += 1
            _append_purpose(existing, query.purpose)
            _record_source_provenance(
                existing,
                source="openai_web_search",
                title=item_data.get("title"),
                url=url,
                published_at=item_data.get("published_at"),
                snippet=guard_content or snippet,
                channel=source_name,
                query=query.query,
            )
            if not existing.snippet and snippet:
                existing.snippet = snippet
            if not existing.content and guard_content:
                existing.content = guard_content
            if not existing.published_at and published_at:
                existing.published_at = published_at
            if not existing.source_name and source_name:
                existing.source_name = source_name
            continue

        row = MediaItem(
            project_id=project.id,
            query_id=query.id,
            title=item_data.get("title") or "Sem título",
            url=url,
            canonical_url=canonical,
            domain=host,
            published_at=published_at,
            snippet=snippet,
            content=guard_content or snippet,
            source_name=source_name,
            search_source="openai_web_search",
            source_provenance=[
                {
                    "source": "openai_web_search",
                    "title": item_data.get("title"),
                    "url": url,
                    "published_at": item_data.get("published_at"),
                    "snippet": snippet,
                    "channel": source_name,
                    "view_count": None,
                    "evidence": evidence,
                    "query": query.query,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            discovery_purposes=[query.purpose],
        )
        db.add(row)
        existing_items[canonical] = row
        added += 1

    if progress_detail:
        progress_detail(
            "OpenAI Web Search concluido: "
            f"{returned} retornado(s), {added} novo(s), {duplicate_hits} duplicado(s), "
            f"{rejected} rejeitado(s) pelo filtro tematico"
        )

    return {
        "added": added,
        "returned": returned,
        "rejected": rejected,
        "duplicates": duplicate_hits,
    }


def _is_tavily_hard_failure(exc: Exception | str) -> bool:
    """Falhas que tornam inutil insistir no Tavily durante a mesma execucao."""
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code in {401, 402, 403, 429}:
        return True

    message = str(exc).casefold()
    signals = (
        "please upgrade",
        "upgrade your plan",
        "request exceeds your plan",
        "exceeds your plan's set usage limit",
        "set usage limit",
        "usage limit",
        "usage_limit",
        "monthly limit",
        "plan limit",
        "quota",
        "insufficient credits",
        "credit limit",
        "rate limit",
        "rate_limit",
        "too many requests",
        "contact support@tavily.com",
        "limit exceeded",
        "exceeded your",
        "invalid api key",
        "unauthorized",
        "forbidden",
    )
    return any(signal in message for signal in signals)


def collect_tavily(
    db: Session,
    project_id: int,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
    stats: dict[str, int] | None = None,
) -> int:
    """Coleta web com Tavily primario e circuit breaker para OpenAI Web Search.

    A primeira consulta real ja funciona como teste do Tavily. Nao existe um
    health-check separado. Enquanto o Tavily responde normalmente ele continua
    sendo usado. Se surgir uma falha dura de plano/quota/autenticacao, o circuito
    e aberto e TODAS as consultas restantes desta execucao vao direto para o
    OpenAI Web Search, sem tentar Tavily novamente.

    Falhas transitórias isoladas nao abrem o circuito: apenas aquela consulta
    usa o fallback e a proxima ainda pode tentar Tavily.
    """
    settings = get_settings()
    tavily_key = settings.tavily_api_key

    client = None
    if tavily_key:
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=tavily_key)
        except Exception:
            client = None

    if client is None and not llm_is_configured():
        raise RuntimeError(
            "Nem TAVILY_API_KEY nem OPENAI_API_KEY estão configuradas para a coleta web"
        )

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

    queries = queries[: max(1, settings.max_search_queries)]
    global_limit = max(1, settings.max_search_results)
    per_query_cap = max(1, settings.max_results_per_query)

    counters = {
        "queries_total": len(queries),
        "queries_successful": 0,
        "tavily_attempts": 0,
        "tavily_queries": 0,
        "tavily_hard_failures": 0,
        "tavily_circuit_breaker_trips": 0,
        "tavily_switched_at_query": 0,
        "web_search_fallback_queries": 0,
        "web_search_fallback_results": 0,
        "web_search_fallback_added": 0,
        "web_search_fallback_rejected": 0,
        "web_search_fallback_zero_accept": 0,
        "failed_queries": 0,
        "global_result_limit": global_limit,
        "global_limit_reached": 0,
        "topic_guard_rejected": 0,
    }
    added = 0
    errors: list[str] = []
    total_queries = len(queries)

    tavily_circuit_open = client is None
    tavily_disable_reason = "TAVILY_API_KEY não configurada" if client is None else None

    configured_budget = int(settings.max_web_search_fallback_queries)
    fallback_budget = (
        max(1, settings.max_search_queries)
        if configured_budget <= 0
        else max(1, min(configured_budget, settings.max_search_queries))
    )

    def open_tavily_circuit(reason: str, query_index: int) -> None:
        nonlocal tavily_circuit_open, tavily_disable_reason, fallback_budget
        if tavily_circuit_open and tavily_disable_reason:
            return
        tavily_circuit_open = True
        tavily_disable_reason = reason[:1000]
        counters["tavily_hard_failures"] += 1
        counters["tavily_circuit_breaker_trips"] += 1
        counters["tavily_switched_at_query"] = query_index
        # Erro duro significa que insistir no Tavily nao faz sentido. Libera
        # todas as consultas restantes para o Web Search, mesmo que um limite
        # de fallback menor tenha sido configurado anteriormente.
        fallback_budget = max(fallback_budget, settings.max_search_queries)
        if progress_detail:
            progress_detail(
                "Tavily respondeu com bloqueio de plano/quota/autenticacao. "
                "Circuit breaker aberto: todas as proximas consultas desta execucao "
                "irao direto para OpenAI Web Search."
            )

    for query_index, query in enumerate(queries, start=1):
        if cancel_check:
            cancel_check()

        start, end = query_window(project, query)
        has_window = _valid_search_window(start, end)
        mode = (
            f"janela {start.isoformat()} a {end.isoformat()}"
            if has_window
            else "busca temática sem filtro temporal"
        )

        remaining_global = global_limit - added
        if remaining_global <= 0:
            counters["global_limit_reached"] = 1
            if progress_detail:
                progress_detail(
                    f"Limite global de {global_limit} novo(s) item(ns) atingido; encerrando a coleta web"
                )
            break

        queries_left = max(1, total_queries - query_index + 1)
        fair_share = max(1, math.ceil(remaining_global / queries_left))
        query_result_limit = min(per_query_cap, fair_share, remaining_global)

        response = None
        tavily_error: str | None = None

        if not tavily_circuit_open and client is not None:
            if progress_detail:
                progress_detail(
                    f"Consulta {query_index}/{total_queries} via Tavily ({mode}): {query.query[:100]}"
                )

            is_youtube_query = query.kind == "youtube"
            params: dict = {
                "query": query.query,
                "max_results": query_result_limit,
                "include_raw_content": "text",
                "topic": "general" if is_youtube_query else (
                    "news" if query.purpose == "MEDIA_REPERCUSSION" else "general"
                ),
                "search_depth": "advanced" if is_youtube_query or query.purpose != "MEDIA_REPERCUSSION" else "basic",
            }
            if is_youtube_query:
                params["include_domains"] = ["youtube.com", "youtu.be"]
            if has_window:
                params["start_date"] = start.isoformat()
                params["end_date"] = end.isoformat()

            counters["tavily_attempts"] += 1
            try:
                response = client.search(**params)
                counters["tavily_queries"] += 1
            except Exception as exc:
                tavily_error = str(exc)

                # Primeiro verifica falha dura. Se for quota/upgrade, NAO faz
                # qualquer segundo teste no Tavily, inclusive retry de data.
                if _is_tavily_hard_failure(exc):
                    open_tavily_circuit(tavily_error, query_index)
                else:
                    message = tavily_error.casefold()
                    date_error = has_window and any(
                        token in message
                        for token in ("start_date", "end_date", "date cannot", "date must", "same")
                    )
                    if date_error:
                        retry_params = dict(params)
                        retry_params.pop("start_date", None)
                        retry_params.pop("end_date", None)
                        if progress_detail:
                            progress_detail(
                                f"Consulta {query_index}/{total_queries}: Tavily rejeitou apenas a janela; "
                                "repetindo a mesma consulta sem filtro temporal"
                            )
                        counters["tavily_attempts"] += 1
                        try:
                            response = client.search(**retry_params)
                            counters["tavily_queries"] += 1
                        except Exception as retry_exc:
                            tavily_error = str(retry_exc)
                            if _is_tavily_hard_failure(retry_exc):
                                open_tavily_circuit(tavily_error, query_index)
        else:
            tavily_error = tavily_disable_reason or "Tavily desativado nesta execução"
            if progress_detail:
                progress_detail(
                    f"Consulta {query_index}/{total_queries} via OpenAI Web Search: {query.query[:100]}"
                )

        if isinstance(response, dict) and response.get("error"):
            tavily_error = str(response.get("error"))
            if _is_tavily_hard_failure(tavily_error):
                open_tavily_circuit(tavily_error, query_index)
            response = None

        if response is not None:
            is_youtube_query = query.kind == "youtube"
            for result in response.get("results", [])[:query_result_limit]:
                url = result.get("url")
                if not url:
                    continue
                if query.purpose == "MEDIA_REPERCUSSION" and not _collection_guard(
                    project,
                    title=result.get("title"),
                    snippet=result.get("content"),
                    content=result.get("raw_content"),
                ):
                    counters["topic_guard_rejected"] += 1
                    continue

                canonical = canonicalize(url)
                existing = existing_items.get(canonical)
                if existing:
                    _append_purpose(existing, query.purpose)
                    _record_source_provenance(
                        existing,
                        source="tavily",
                        title=result.get("title"),
                        url=url,
                        published_at=result.get("published_date"),
                        snippet=result.get("content"),
                        channel=None if is_youtube_query else result.get("source"),
                        query=query.query,
                    )
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
                    source_name=None if is_youtube_query else result.get("source"),
                    search_source="tavily",
                    source_provenance=[
                        {
                            "source": "tavily",
                            "title": result.get("title"),
                            "url": url,
                            "published_at": str(result.get("published_date")) if result.get("published_date") else None,
                            "snippet": result.get("content"),
                            "channel": None if is_youtube_query else result.get("source"),
                            "query": query.query,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                    discovery_purposes=[query.purpose],
                )
                db.add(row)
                existing_items[canonical] = row
                added += 1

            query.executed_at = datetime.now(timezone.utc)
            counters["queries_successful"] += 1
            db.commit()
            continue

        # Sem resposta Tavily: a consulta corrente e todas as seguintes (quando
        # o circuito estiver aberto) sao executadas no Web Search.
        if llm_is_configured() and counters["web_search_fallback_queries"] < fallback_budget:
            if progress_detail and not tavily_circuit_open:
                reason = (tavily_error or "falha não especificada")[:160]
                progress_detail(
                    f"Consulta {query_index}/{total_queries}: Tavily falhou ({reason}); "
                    "OpenAI Web Search assumindo apenas esta consulta"
                )
            try:
                fallback_result = _collect_openai_web_search_query(
                    db,
                    project,
                    query,
                    existing_items,
                    max_results=query_result_limit,
                    cancel_check=cancel_check,
                    progress_detail=progress_detail,
                )
                fallback_added = int(fallback_result.get("added", 0))
                added += fallback_added
                counters["web_search_fallback_queries"] += 1
                counters["web_search_fallback_results"] += int(fallback_result.get("returned", 0))
                counters["web_search_fallback_added"] += fallback_added
                counters["web_search_fallback_rejected"] += int(fallback_result.get("rejected", 0))
                if fallback_added == 0:
                    counters["web_search_fallback_zero_accept"] += 1
                query.executed_at = datetime.now(timezone.utc)
                counters["queries_successful"] += 1
                db.commit()
                continue
            except RuntimeError as web_exc:
                db.rollback()
                counters["web_search_fallback_queries"] += 1
                counters["failed_queries"] += 1
                errors.append(
                    f"{query.query}: OpenAI Web Search={web_exc}"
                    + (f" | Tavily={tavily_error}" if not tavily_circuit_open else "")
                )
                if progress_detail:
                    progress_detail(
                        f"Consulta {query_index}/{total_queries}: OpenAI Web Search falhou; seguindo para a próxima"
                    )
                continue

        if llm_is_configured() and counters["web_search_fallback_queries"] >= fallback_budget:
            counters["failed_queries"] += 1
            errors.append(
                f"{query.query}: orçamento de fallback OpenAI ({fallback_budget}) atingido"
            )
            if progress_detail:
                progress_detail(
                    f"Consulta {query_index}/{total_queries}: orçamento de Web Search atingido"
                )
            continue

        counters["failed_queries"] += 1
        errors.append(
            f"{query.query}: Tavily indisponível e OPENAI_API_KEY não configurada para fallback"
        )

    if stats is not None:
        stats.clear()
        stats.update(counters)

    if total_queries and counters["queries_successful"] == 0 and errors:
        summary = " | ".join(errors[:3])
        if tavily_circuit_open:
            raise RuntimeError(
                "Tavily foi desativado nesta execução e o OpenAI Web Search também não conseguiu "
                f"concluir nenhuma consulta. {summary}"[:2000]
            )
        raise RuntimeError(f"Coleta web indisponível. {summary}"[:2000])

    return added


def collect_youtube_api(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> int:
    """Coleta preferencial pela YouTube Data API v3.

    Se a API estiver sem chave, sem quota ou indisponível, o chamador deve
    usar o agente OpenAI Web Search como fallback.
    """
    settings = get_settings()
    if not settings.youtube_api_key:
        raise RuntimeError("YOUTUBE_API_KEY não configurada")

    import json
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    tasks = youtube_tasks_for_execution(project)
    start, end = media_window(project)
    has_window = _valid_search_window(start, end)
    existing_items = {
        item.canonical_url: item
        for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    }

    def api_get(endpoint: str, params: dict[str, object]) -> dict:
        params = {**params, "key": settings.youtube_api_key}
        url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urlencode(params)}"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "ISP-Repercussao/0.2"})
        try:
            with urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = str(exc.reason)
            raise RuntimeError(f"YouTube Data API indisponível (HTTP {exc.code}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"YouTube Data API indisponível: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("YouTube Data API indisponível: timeout") from exc

    added = 0
    total_limit = max(1, settings.max_youtube_results_total)
    per_task_cap = max(1, settings.max_youtube_results_per_task)
    total_tasks = len(tasks)
    for task_index, task in enumerate(tasks, start=1):
        if added >= total_limit:
            if progress_detail:
                progress_detail(f"Limite global do YouTube ({total_limit}) atingido")
            break
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"YouTube API {task_index}/{total_tasks}: {task.target}")

        search_params: dict[str, object] = {
            "part": "snippet",
            "type": "video",
            "q": task.query,
            "maxResults": min(
                50,
                settings.youtube_web_search_max_results,
                per_task_cap,
                max(1, total_limit - added),
            ),
            "order": "relevance",
        }
        if has_window:
            search_params["publishedAfter"] = f"{start.isoformat()}T00:00:00Z"
            search_params["publishedBefore"] = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
        search_data = api_get("search", search_params)
        ids = [
            str(row.get("id", {}).get("videoId"))
            for row in search_data.get("items", [])
            if row.get("id", {}).get("videoId")
        ]
        if not ids:
            continue

        details = api_get(
            "videos",
            {"part": "snippet,statistics", "id": ",".join(ids), "maxResults": len(ids)},
        )
        by_id = {str(row.get("id")): row for row in details.get("items", []) if row.get("id")}

        for video_id in ids:
            if added >= total_limit:
                break
            raw = by_id.get(video_id)
            if not raw:
                continue
            snippet = raw.get("snippet") or {}
            stats = raw.get("statistics") or {}
            published_at = result_publication_date(snippet.get("publishedAt"))
            if has_window and published_at and not (start <= published_at <= end):
                continue
            try:
                view_count = int(stats["viewCount"]) if stats.get("viewCount") is not None else None
            except (TypeError, ValueError):
                view_count = None

            url = f"https://www.youtube.com/watch?v={video_id}"
            title = str(snippet.get("title") or "Vídeo sem título")
            description = snippet.get("description") or None
            channel = snippet.get("channelTitle") or "YouTube"
            if not _collection_guard(
                project, title=title, snippet=description, content=description
            ):
                continue
            canonical = canonicalize(url)
            existing = existing_items.get(canonical)
            if existing:
                _record_source_provenance(
                    existing, source="youtube_api", title=title, url=url,
                    published_at=snippet.get("publishedAt"), snippet=description,
                    channel=channel, view_count=view_count,
                )
                existing.title = title or existing.title
                existing.published_at = published_at or existing.published_at
                existing.snippet = description or existing.snippet
                existing.content = description or existing.content
                existing.source_name = channel or existing.source_name
                existing.view_count = view_count if view_count is not None else existing.view_count
                existing.search_source = "youtube_api"
                _append_purpose(existing, "MEDIA_REPERCUSSION")
                continue

            row = MediaItem(
                project_id=project.id, title=title, url=url, canonical_url=canonical,
                domain="www.youtube.com", published_at=published_at, snippet=description,
                content=description, source_name=channel, view_count=view_count,
                search_source="youtube_api",
                source_provenance=[{
                    "source": "youtube_api", "title": title, "url": url,
                    "published_at": snippet.get("publishedAt"), "snippet": description,
                    "channel": channel, "view_count": view_count,
                }],
                discovery_purposes=["MEDIA_REPERCUSSION"],
            )
            db.add(row)
            existing_items[canonical] = row
            added += 1

    db.commit()
    return added


def collect_youtube_web_search(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> int:
    settings = get_settings()
    tasks = youtube_tasks_for_execution(project)
    total_limit = max(1, settings.max_youtube_results_total)
    per_task_cap = max(1, settings.max_youtube_results_per_task)
    start, end = media_window(project)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "videos": {
                "type": "array",
                "maxItems": max(1, min(15, per_task_cap)),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "channel": {"type": ["string", "null"]},
                        "published_at": {"type": ["string", "null"]},
                        "description": {"type": ["string", "null"]},
                        "view_count": {"type": ["integer", "null"], "minimum": 0},
                    },
                    "required": ["title", "url", "channel", "published_at", "description", "view_count"],
                },
            }
        },
        "required": ["videos"],
    }
    instructions = """Você é um agente de pesquisa de repercussão midiática no YouTube.
Use obrigatoriamente a ferramenta de pesquisa web disponível. Retorne somente vídeos reais
e pertinentes à consulta. Quando collection_start e collection_end estiverem preenchidos e
formarem uma janela válida, respeite essa janela. Quando estiverem nulos, não invente datas:
faça uma busca temática usando o assunto, entidades, ano e contexto presentes na consulta.
Não invente nem complete campos ausentes. A URL deve ser a página do vídeo no YouTube (youtube.com ou youtu.be), não
uma página de resultado, canal, playlist, Short sem URL de vídeo verificável, nem fonte externa.
Mantenha título, canal, data, descrição e visualizações fiéis à página encontrada. O campo `title`
deve ser o título real do vídeo; não use apenas uma data, o nome do canal ou texto genérico como
título. Se o título real não puder ser recuperado com segurança, descarte o resultado em vez de
inventar um título. Retorne `view_count` somente como inteiro quando a página ou o resultado da
busca exibir uma contagem explícita; em qualquer outro caso use null. Não estime, arredonde nem
converta abreviações ambíguas. Retorne apenas o JSON no formato solicitado; uma lista vazia é
preferível a um resultado duvidoso."""

    total_tasks = len(tasks)
    existing_items = {
        item.canonical_url: item
        for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    }
    added = 0
    for task_index, task in enumerate(tasks, start=1):
        is_priority_task = task.target != "Busca temática"
        if added >= total_limit and not is_priority_task:
            if progress_detail:
                progress_detail(f"Limite global do YouTube ({total_limit}) atingido")
            break
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Busca {task_index}/{total_tasks} no YouTube: {task.target}")
        result = web_search_structured_response(
            instructions=instructions,
            payload={
                "query": task.query,
                "target": task.target,
                "rationale": task.rationale,
                "collection_start": start.isoformat() if start else None,
                "collection_end": end.isoformat() if end else None,
                "temporal_mode": "explicit_window" if _valid_search_window(start, end) else "topic_driven",
            },
            schema_name="youtube_video_search",
            schema=schema,
            allowed_domains=["youtube.com", "youtu.be"],
            model=settings.youtube_search_model,
            retry_without_domain_filter=True,
        )
        task_limit = min(
            settings.youtube_web_search_max_results,
            1 if is_priority_task else per_task_cap,
            max(1 if is_priority_task else 0, total_limit - added),
        )
        for video in result["videos"][:task_limit]:
            if added >= total_limit:
                break
            url = video["url"].strip()
            if not is_youtube_url(url):
                continue
            if is_priority_task and not matches_priority_youtube_channel(video.get("channel"), task.target):
                # Mencionar o canal no título/descrição não prova que o vídeo
                # foi publicado por ele; sem a identidade do canal, descarta.
                continue
            if not _collection_guard(
                project,
                title=video.get("title"),
                snippet=video.get("description"),
                content=video.get("description"),
            ):
                continue
            raw_view_count = video.get("view_count")
            view_count = (
                raw_view_count
                if isinstance(raw_view_count, int) and not isinstance(raw_view_count, bool) and raw_view_count >= 0
                else None
            )
            canonical = canonicalize(url)
            existing = existing_items.get(canonical)
            if existing:
                _record_source_provenance(
                    existing,
                    source="openai_web_search",
                    title=video["title"],
                    url=url,
                    published_at=video["published_at"],
                    snippet=video["description"],
                    channel=video["channel"],
                    view_count=view_count,
                    query=task.query,
                    target=task.target,
                )
                existing.title = video["title"] or existing.title
                existing.published_at = result_publication_date(video["published_at"]) or existing.published_at
                existing.snippet = video["description"] or existing.snippet
                existing.content = video["description"] or existing.content
                existing.source_name = video["channel"] or existing.source_name
                existing.view_count = view_count if view_count is not None else existing.view_count
                existing.search_source = "openai_web_search"
                _append_purpose(existing, "MEDIA_REPERCUSSION")
                continue

            description = video["description"]
            row = MediaItem(
                project_id=project.id,
                title=video["title"] or "Vídeo sem título",
                url=url,
                canonical_url=canonical,
                domain=urlparse(url).netloc.lower(),
                published_at=result_publication_date(video["published_at"]),
                snippet=description,
                content=description,
                source_name=video["channel"] or "YouTube",
                view_count=view_count,
                search_source="openai_web_search",
                source_provenance=[
                    {
                        "source": "openai_web_search",
                        "title": video["title"],
                        "url": url,
                        "published_at": video["published_at"],
                        "snippet": description,
                        "channel": video["channel"],
                        "view_count": view_count,
                        "query": task.query,
                        "target": task.target,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    }
                ],
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
    enable_youtube: bool = True,
    cancel_check: Callable[[], None] | None = None,
    web_progress: Callable[[str], None] | None = None,
    youtube_progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Executa coletores independentes e devolve o resultado de cada agente.

    A coleta Tavily termina antes da coleta no YouTube para que URLs idênticas
    sejam mescladas no mesmo item e possam receber validação cruzada. Os agentes
    e seus tratamentos isolados de falhas continuam independentes.
    """

    def web_agent() -> dict[str, object]:
        session = SessionLocal()
        try:
            web_stats: dict[str, int] = {}
            collected = collect_tavily(
                session,
                project.id,
                cancel_check=cancel_check,
                progress_detail=web_progress,
                stats=web_stats,
            )
            fallback_queries = int(web_stats.get("web_search_fallback_queries", 0))
            failed_queries = int(web_stats.get("failed_queries", 0))
            status = "COMPLETED"
            if failed_queries:
                status = "PARTIAL"
            elif fallback_queries:
                status = "FALLBACK_WEB_SEARCH"
            return {
                "status": status,
                "collected": collected,
                "error": None,
                "provider": "tavily+openai_web_search" if fallback_queries else "tavily",
                "stats": web_stats,
            }
        except RuntimeError as exc:
            session.rollback()
            return {
                "status": "UNAVAILABLE",
                "collected": 0,
                "error": str(exc)[:1000],
                "provider": None,
                "stats": {},
            }
        finally:
            session.close()

    def youtube_agent() -> dict[str, object]:
        if not enable_youtube:
            return {"status": "DISABLED", "collected": 0, "error": None, "provider": None}

        session = SessionLocal()
        try:
            local_project = session.get(Project, project.id)
            if not local_project:
                raise RuntimeError("Projeto não encontrado")

            if llm_is_configured():
                try:
                    collected = collect_youtube_web_search(
                        session, local_project, cancel_check=cancel_check,
                        progress_detail=youtube_progress,
                    )
                    return {
                        "status": "COMPLETED",
                        "collected": collected, "error": None, "provider": "openai_web_search",
                    }
                except RuntimeError as exc:
                    session.rollback()
                    return {"status": "UNAVAILABLE", "collected": 0, "error": str(exc)[:1000], "provider": "openai_web_search"}

            return {"status": "NOT_CONFIGURED", "collected": 0, "error": "OPENAI_API_KEY não configurada para a pesquisa no YouTube.", "provider": None}
        finally:
            session.close()

    # Os agentes são independentes: uma falha na busca web não impede o agente
    # do YouTube de usar a API oficial ou o fallback OpenAI Web Search.
    web = web_agent()
    youtube = youtube_agent()

    return {
        "web": web,
        "youtube": youtube,
        "parallel": False,
    }


def validate_tavily_youtube_metadata(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict[str, int | bool]:
    """Compara metadados do mesmo vídeo obtidos por Tavily e OpenAI Web Search."""
    if not llm_is_configured():
        return {"validated": 0, "skipped": True}

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["CONFIRMED", "PARTIALLY_CONFIRMED", "CONFLICT", "INSUFFICIENT_EVIDENCE"],
            },
            "matching_fields": {"type": "array", "items": {"type": "string"}},
            "conflicting_fields": {"type": "array", "items": {"type": "string"}},
            "detail": {"type": "string"},
        },
        "required": ["status", "matching_fields", "conflicting_fields", "detail"],
    }
    instructions = """Você é um agente de validação cruzada de metadados de vídeos.
Compare exclusivamente os dois registros recebidos: um obtido pelo Tavily e outro pela
OpenAI Web Search restrita ao YouTube. Não pesquise a web e não infira dados ausentes.
Considere a URL canônica igual como confirmação da identidade do vídeo. Compare título,
canal, data de publicação, descrição e visualizações apenas quando ambos os registros trouxerem
o campo. Visualizações são uma fotografia no tempo: considere compatível uma diferença de até
10% ou 5.000 visualizações, o que for maior; não compare se qualquer fonte omitir a contagem.
Use INSUFFICIENT_EVIDENCE se somente a URL puder ser comparada; PARTIALLY_CONFIRMED se
ao menos um metadado adicional concordar sem conflito; CONFLICT se houver divergência
material em qualquer campo comparável; CONFIRMED se todos os campos comparáveis
concordarem. Explique em português de forma curta e factual."""

    candidates = []
    for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all():
        sources = {row.get("source"): row for row in (item.source_provenance or [])}
        if "tavily" in sources and "openai_web_search" in sources:
            candidates.append((item, sources["tavily"], sources["openai_web_search"]))

    if not candidates:
        return {
            "validated": 0,
            "skipped": True,
            "reason": "NO_COMPARABLE_ITEMS",
        }

    settings = get_settings()
    validation_cap = max(1, settings.max_cross_validations)
    validated = 0
    for index, (item, tavily, web_search) in enumerate(candidates, start=1):
        if validated >= validation_cap:
            break
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Vídeo {index}/{len(candidates)}: {item.title[:90]}")
        result = structured_response(
            instructions=instructions,
            payload={"canonical_url": item.canonical_url, "tavily": tavily, "openai_web_search": web_search},
            schema_name="youtube_cross_validation",
            schema=schema,
            max_output_tokens=700,
        )
        item.cross_validation_status = result["status"]
        item.cross_validation_detail = result["detail"][:1000]
        validated += 1
    db.commit()
    return {"validated": validated, "skipped": False, "reason": None}


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


def _institutional_anchor_score(project: Project, body: str) -> int:
    """Heurística conservadora para produto institucional quando o LLM falha.

    Tema semelhante não basta. Exigimos referência ao produto ou à instituição
    combinada com termos distintivos do produto.
    """
    haystack = normalized_text(body)
    topic = normalized_text(project.topic)
    topic_without_year = re.sub(r"\b20\d{2}\b", "", topic)
    topic_without_year = " ".join(topic_without_year.split())
    score = 0

    if topic and topic in haystack:
        score += 3
    elif len(topic_without_year) >= 8 and topic_without_year in haystack:
        score += 2

    institution_terms = {
        normalized_text(project.institution or ""),
        "instituto de seguranca publica",
    }
    profile = project.topic_profile or {}
    institution_terms.update(normalized_text(x) for x in (profile.get("organizations") or []) if x)
    institution_hit = any(term and len(term) >= 5 and term in haystack for term in institution_terms)
    acronym_hit = bool(re.search(r"\bisp(?:-rj)?\b", haystack))
    if institution_hit or acronym_hit:
        score += 1

    generic = {
        "dossie", "relatorio", "boletim", "anuario", "estudo", "publicacao",
        "instituto", "seguranca", "publica", "rio", "janeiro", "2026",
    }
    core_terms = {term for term in normalized_terms(project.topic) if term not in generic}
    body_terms = normalized_terms(body)
    if len(core_terms.intersection(body_terms)) >= 2:
        score += 1
    return score


def _relevance_fallback_decision(project: Project, item: MediaItem) -> tuple[bool, str]:
    body = " ".join(filter(None, [item.title, item.snippet, item.content or ""]))
    fallback_score = _topic_overlap_score(project, body)
    institutional_score = (
        _institutional_anchor_score(project, body)
        if project.project_type == "INSTITUTIONAL_PRODUCT"
        else 0
    )
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        related = institutional_score >= 2
        return (
            related,
            "Âncora documental/institucional suficiente"
            if related
            else "Tema semelhante, mas sem vínculo documental suficiente com o produto institucional",
        )
    return (
        fallback_score > 0,
        "Aderência lexical" if fallback_score > 0 else "Sem aderência lexical suficiente",
    )


def _review_media_relevance_batch(
    project: Project,
    items: list[MediaItem],
) -> tuple[dict[int, tuple[bool, str]], int]:
    """Revisa vários itens em uma única chamada estruturada.

    Retorna ``({media_item_id: (related, evidence)}, llm_calls)``. Se a OpenAI
    estiver indisponível, a decisão cai para a heurística conservadora sem
    abrir uma chamada por item.
    """
    if not items:
        return {}, 0

    settings = get_settings()
    if not llm_is_configured():
        return {
            item.id: _relevance_fallback_decision(project, item)
            for item in items
        }, 0

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array",
                "maxItems": len(items),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "media_item_id": {"type": "integer"},
                        "related": {"type": "boolean"},
                        "relation_type": {
                            "type": "string",
                            "enum": [
                                "DIRECT_PRODUCT",
                                "ATTRIBUTED_FINDING",
                                "DIRECT_EVENT",
                                "THEMATIC_ONLY",
                                "UNRELATED",
                            ],
                        },
                        "anchor": {"type": "string"},
                        "evidence": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "media_item_id",
                        "related",
                        "relation_type",
                        "anchor",
                        "evidence",
                        "reason",
                    ],
                },
            }
        },
        "required": ["decisions"],
    }

    instructions = MEDIA_RELEVANCE_PROMPT + """

Você receberá vários itens na mesma chamada. Avalie CADA item de forma independente.
Não deixe a evidência de um item contaminar a decisão de outro. Preserve exatamente o
media_item_id recebido e retorne uma decisão para cada item. Não invente conteúdo ausente.
"""
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        instructions += """

REGRA OBRIGATÓRIA PARA PRODUTO INSTITUCIONAL:
O objeto desta análise é um produto institucional específico e, quando houver ano/edição no nome, essa edição faz parte da identidade do produto.

Um item só pode ter related=true quando houver vínculo documental verificável com O PRODUTO SOLICITADO.

Aceite somente:
1. DIRECT_PRODUCT: o item menciona explicitamente o produto solicitado ou sua edição correta; ou
2. ATTRIBUTED_FINDING: o item apresenta dado, conclusão, estatística ou achado e atribui explicitamente esse conteúdo ao produto solicitado ou ao ISP em contexto inequívoco da mesma edição.

REJEITE:
- outra edição do produto;
- Dossiê Mulher 2025 quando o objeto for Dossiê Mulher 2026;
- Dossiê Mulher 2024 quando o objeto for Dossiê Mulher 2026;
- matéria genérica sobre violência contra a mulher;
- caso individual de violência sem relação documental com o produto;
- matéria que apenas menciona ISP sem atribuir ao produto analisado o dado ou conclusão;
- conteúdo que compartilha apenas palavras ou temas do produto.

Uma edição anterior só pode ser aceita em uma matéria comparativa quando a edição alvo também estiver explicitamente presente e for parte material da análise.
THEMATIC_ONLY sempre implica related=false.
Não presuma que uma matéria pertence ao Dossiê apenas porque aborda violência contra mulheres no Rio de Janeiro.
O campo anchor deve identificar a evidência textual concreta que conecta o item ao produto e à edição solicitada.
"""

    payload_items = []
    max_chars = max(500, settings.validation_item_max_chars)
    for item in items:
        payload_items.append(
            {
                "media_item_id": item.id,
                "title": item.title,
                "snippet": item.snippet,
                "content": (item.content or "")[:max_chars],
                "source": item.source_name or item.domain,
                "url": item.url,
            }
        )

    try:
        result = structured_response(
            instructions=instructions,
            payload={
                "topic": project.topic,
                "project_type": project.project_type,
                "institution": project.institution,
                "product_name": (project.topic_profile or {}).get("product_name"),
                "product_anchor": (project.topic_profile or {}).get("product_anchor"),
                "product_search_variants": (project.topic_profile or {}).get("product_search_variants", []),
                "topic_profile": project.topic_profile,
                "items": payload_items,
            },
            schema_name="media_relevance_batch_v1",
            schema=schema,
            max_output_tokens=max(2200, min(9000, 700 * len(items))),
        )
    except RuntimeError:
        return {
            item.id: _relevance_fallback_decision(project, item)
            for item in items
        }, 1

    by_id = {
        int(row["media_item_id"]): row
        for row in result.get("decisions", [])
        if row.get("media_item_id") is not None
    }
    allowed_types = (
        {"DIRECT_PRODUCT", "ATTRIBUTED_FINDING"}
        if project.project_type == "INSTITUTIONAL_PRODUCT"
        else {"DIRECT_PRODUCT", "ATTRIBUTED_FINDING", "DIRECT_EVENT"}
    )

    decisions: dict[int, tuple[bool, str]] = {}
    for item in items:
        row = by_id.get(item.id)
        if not row:
            decisions[item.id] = _relevance_fallback_decision(project, item)
            continue
        related = bool(row.get("related")) and row.get("relation_type") in allowed_types
        evidence = (
            row.get("anchor")
            or row.get("evidence")
            or row.get("reason")
            or "Sem âncora temática"
        )
        decisions[item.id] = (related, str(evidence))
    return decisions, 1

def _mentions_institution(project: Project, body: str) -> bool:
    text = normalized_text(body)
    full_name = normalized_text(project.institution or "")
    return bool(
        (full_name and full_name in text)
        or "instituto de seguranca publica" in text
        or re.search(r"\bisp(?:-rj)?\b", text)
    )


def _low_information_title(title: str | None) -> bool:
    text = " ".join((title or "").split()).strip()
    if not text:
        return True
    normalized = normalized_text(text)
    if re.fullmatch(r"\d{1,2}\s+de\s+[a-z]+\s+de\s+20\d{2}", normalized):
        return True
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", normalized):
        return True
    return len(normalized_terms(text)) < 2

def validate_and_classify(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    settings = get_settings()
    review_cap = max(1, settings.max_semantic_reviews)
    batch_size = max(1, settings.validation_batch_size)
    use_llm = llm_is_configured()

    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    start, end = media_window(project)
    enforce_window = bool(project.has_custom_date_window and start and end)

    valid = 0
    discarded = 0
    semantic_reviews = 0
    llm_calls = 0
    already_decided = 0
    pending_after_quota = 0
    candidates: list[MediaItem] = []

    total_items = len(items)
    for item_index, item in enumerate(items, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Pré-triagem {item_index}/{total_items}: {item.title[:90]}")

        if item.status != "PENDING":
            already_decided += 1
            continue

        publication_date = inferred_publication_date(item)
        is_social = (
            item.search_source in {"youtube", "youtube_api", "openai_web_search"}
            or is_youtube_host(item.domain or "")
        )

        if enforce_window and is_social and not publication_date:
            item.status = "DATE_UNVERIFIED"
            item.discard_reason = (
                "Data de publicação do vídeo não verificável dentro de uma pauta com janela temporal"
            )
            discarded += 1
            continue

        if enforce_window and publication_date and not start <= publication_date <= end:
            item.status = "OUTSIDE_COLLECTION_WINDOW"
            item.discard_reason = (
                f"Publicado em {publication_date.isoformat()}, fora da janela midiática "
                f"{start.isoformat()} a {end.isoformat()}"
            )
            discarded += 1
            continue

        # Produto institucional com edição/ano: rejeita explicitamente a edição
        # errada antes de qualquer chamada semântica.
        if project.project_type == "INSTITUTIONAL_PRODUCT":
            version_ok, version_reason = _institutional_product_version_guard_text(
                project,
                title=item.title,
                snippet=item.snippet,
                content=(item.content or "")[: settings.validation_item_max_chars],
            )
            if not version_ok:
                item.status = "NOT_RELATED"
                item.discard_reason = (version_reason or "Edição incompatível")[:1000]
                discarded += 1
                if progress_detail:
                    progress_detail(
                        f"Descartado antes da IA: {item.title[:80]} · edição incompatível"
                    )
                continue

        # Produtos institucionais e eventos factuais ancorados recebem um
        # pré-filtro determinístico antes da chamada LLM. Isso elimina ruído
        # grosseiro sem gastar revisão semântica.
        if not _collection_guard(
            project,
            title=item.title,
            snippet=item.snippet,
            content=(item.content or "")[: settings.validation_item_max_chars],
        ):
            item.status = "NOT_RELATED"
            item.discard_reason = (
                "Pré-filtro temático: item não preserva a âncora nominal/factual "
                "do objeto monitorado"
            )
            discarded += 1
            continue

        candidates.append(item)

    db.commit()

    if use_llm and len(candidates) > review_cap:
        pending_after_quota = len(candidates) - review_cap
        candidates = candidates[:review_cap]

    total_batches = max(1, math.ceil(len(candidates) / batch_size)) if candidates else 0
    for batch_index, offset in enumerate(range(0, len(candidates), batch_size), start=1):
        if cancel_check:
            cancel_check()
        batch = candidates[offset : offset + batch_size]
        if progress_detail:
            progress_detail(
                f"Validação semântica em lote {batch_index}/{total_batches}: "
                f"{len(batch)} item(ns)"
            )

        decisions, calls = _review_media_relevance_batch(project, batch)
        llm_calls += calls
        semantic_reviews += len(batch)

        for item in batch:
            related, evidence = decisions.get(
                item.id,
                _relevance_fallback_decision(project, item),
            )
            if not related:
                item.status = "NOT_RELATED"
                item.discard_reason = evidence[:1000]
                discarded += 1
                continue

            item.status = "VALID"
            item.discard_reason = None
            mention = _mentions_institution(
                project,
                " ".join(filter(None, [item.title, item.snippet, item.content])),
            )
            classification = db.scalar(
                select(Classification).where(Classification.media_item_id == item.id)
            )
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

    # Sem LLM, o cap de revisões não deve bloquear a heurística local.
    if not use_llm and candidates:
        pending_after_quota = 0

    if not project.has_custom_date_window:
        valid_items = db.scalars(
            select(MediaItem).where(
                MediaItem.project_id == project.id,
                MediaItem.status == "VALID",
            )
        ).all()
        observed_dates = [
            inferred_publication_date(item)
            for item in valid_items
            if inferred_publication_date(item) is not None
        ]
        if observed_dates:
            observed_start = min(observed_dates)
            observed_end = max(observed_dates)
            project.collection_start = observed_start
            project.collection_end = observed_end
            profile_state = dict(project.topic_profile or {})
            profile_state["observed_collection_start"] = observed_start.isoformat()
            profile_state["observed_collection_end"] = observed_end.isoformat()
            project.topic_profile = profile_state
            db.commit()

    return {
        "valid": valid,
        "discarded": discarded,
        "semantic_reviews": semantic_reviews,
        "llm_reviews": semantic_reviews if use_llm else 0,
        "llm_calls": llm_calls,
        "batch_size": batch_size,
        "batches": total_batches,
        "already_decided": already_decided,
        "pending_after_quota": pending_after_quota,
        "temporal_mode": "explicit_window" if enforce_window else "topic_driven",
    }


def classify_with_llm(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    if not llm_is_configured():
        return {
            "updated": 0,
            "skipped": True,
            "already_classified": 0,
            "llm_calls": 0,
            "batches": 0,
        }

    facts = db.scalars(select(OfficialFact).where(OfficialFact.project_id == project.id)).all()
    resolved_facts = fact_events_for_main_report(db, project.id)
    settings = get_settings()
    classification_cap = max(1, settings.max_classifications)
    batch_size = max(1, settings.classification_batch_size)
    max_chars = max(500, settings.classification_item_max_chars)

    items = db.scalars(
        select(MediaItem).where(
            MediaItem.project_id == project.id,
            MediaItem.status == "VALID",
        )
    ).all()

    classifications = {
        row.media_item_id: row
        for row in db.scalars(
            select(Classification).where(
                Classification.media_item_id.in_([item.id for item in items])
            )
        ).all()
    } if items else {}

    pending_items: list[MediaItem] = []
    already_classified = 0
    for item in items:
        classification = classifications.get(item.id)
        if classification and classification.fidelity_status != "PENDENTE":
            already_classified += 1
            continue
        pending_items.append(item)

    pending_after_quota = max(0, len(pending_items) - classification_cap)
    pending_items = pending_items[:classification_cap]

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

    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "media_item_id": {"type": "integer"},
            "theme": {"type": "string"},
            "framing": {"type": "string"},
            "isp_mentioned": {"type": "boolean"},
            "tone_toward_institution": {
                "type": "string",
                "enum": ["POSITIVO", "NEUTRO", "NEGATIVO", "INVERIFICÁVEL"],
            },
            "fidelity_status": {
                "type": "string",
                "enum": ["FIEL", "DIVERGENTE", "INVERIFICÁVEL"],
            },
            "evidence": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "media_item_id",
            "theme",
            "framing",
            "isp_mentioned",
            "tone_toward_institution",
            "fidelity_status",
            "evidence",
            "errors",
        ],
    }

    updated = 0
    llm_calls = 0
    failed_batches = 0
    total_batches = max(1, math.ceil(len(pending_items) / batch_size)) if pending_items else 0

    instructions = ANALYST_PROMPT + """

Você receberá vários itens de mídia na mesma chamada. Classifique CADA item de forma independente.
Não use evidência de um item para classificar outro. Preserve exatamente o media_item_id recebido.
Retorne uma classificação para cada item apresentado. Se a evidência for insuficiente para qualquer
campo, use INVERIFICÁVEL em vez de preencher por inferência.
"""

    for batch_index, offset in enumerate(range(0, len(pending_items), batch_size), start=1):
        if cancel_check:
            cancel_check()
        batch = pending_items[offset : offset + batch_size]
        if progress_detail:
            progress_detail(
                f"Classificação em lote {batch_index}/{total_batches}: {len(batch)} item(ns)"
            )

        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "classifications": {
                    "type": "array",
                    "maxItems": len(batch),
                    "items": item_schema,
                }
            },
            "required": ["classifications"],
        }

        try:
            result = structured_response(
                instructions=instructions,
                payload={
                    "official_facts": official_facts,
                    "resolved_facts": resolved_facts,
                    "media_items": [
                        {
                            "id": item.id,
                            "title": item.title,
                            "url": item.url,
                            "snippet": item.snippet,
                            "content": (item.content or "")[:max_chars],
                        }
                        for item in batch
                    ],
                },
                schema_name="media_classification_batch_v1",
                schema=schema,
                max_output_tokens=max(2600, min(12000, 900 * len(batch))),
            )
            llm_calls += 1
        except RuntimeError:
            # Não transforma uma falha de lote em N chamadas individuais. Mantém
            # as classificações pendentes para uma reexecução futura e segue.
            llm_calls += 1
            failed_batches += 1
            continue

        by_id = {
            int(row["media_item_id"]): row
            for row in result.get("classifications", [])
            if row.get("media_item_id") is not None
        }

        for item in batch:
            row = by_id.get(item.id)
            if not row:
                continue
            classification = classifications.get(item.id)
            payload = {
                key: value
                for key, value in row.items()
                if key != "media_item_id"
            }
            if not classification:
                classification = Classification(media_item_id=item.id, **payload)
                db.add(classification)
                classifications[item.id] = classification
            else:
                for field, value in payload.items():
                    setattr(classification, field, value)
            updated += 1
        db.commit()

    return {
        "updated": updated,
        "skipped": False,
        "already_classified": already_classified,
        "pending_after_quota": pending_after_quota,
        "llm_calls": llm_calls,
        "batch_size": batch_size,
        "batches": total_batches,
        "failed_batches": failed_batches,
    }

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
    youtube_disabled = youtube_status == "DISABLED"

    executed_query_texts = [
        (row.query or "").casefold()
        for row in db.scalars(
            select(SearchQuery).where(
                SearchQuery.project_id == project_id,
                SearchQuery.executed_at.is_not(None),
            )
        ).all()
    ]

    portal_checks = []
    for portal, domain in PRIORITY_PORTALS:
        found = sum(item == domain or item.endswith("." + domain) for item in domains)
        individually_queried = any(f"site:{domain}" in query for query in executed_query_texts)
        if portal == "YouTube" and youtube_disabled:
            result = "coleta desativada para este perfil"
            evidence = "O YouTube não integrou o escopo desta execução; não é possível inferir presença ou ausência de cobertura."
        elif portal == "YouTube" and youtube_unavailable:
            result = "coleta indisponível nesta execução"
            evidence = (
                f"{found} item(ns) de coletas anteriores foram preservados; a indisponibilidade não indica ausência de cobertura."
                if found
                else "A indisponibilidade da pesquisa web não permite concluir ausência de cobertura no YouTube."
            )
        elif found:
            result = "com cobertura auditável"
            evidence = f"{found} item(ns) validado(s) no domínio da amostra."
        elif portal != "YouTube" and not individually_queried:
            result = "não consultado individualmente nesta execução"
            evidence = (
                "O orçamento de consultas foi priorizado entre busca temática, descoberta factual e fontes oficiais; "
                "este portal não recebeu uma consulta site: dedicada nesta execução."
            )
        else:
            result = "sem item validado na amostra"
            evidence = "Nenhum item validado nesse domínio no corpus coletado."
        portal_checks.append(
            {
                "portal": portal,
                "result": result,
                "evidence": evidence,
            }
        )

    collection_days = 0
    if project:
        dated_valid_items = [
            inferred_publication_date(item)
            for item in db.scalars(
                select(MediaItem).where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
            ).all()
        ]
        dated_valid_items = [value for value in dated_valid_items if value is not None]
        if project.has_custom_date_window:
            collection_days = (project.collection_end - project.collection_start).days + 1
        elif dated_valid_items:
            collection_days = (max(dated_valid_items) - min(dated_valid_items)).days + 1
    youtube_note = None
    if youtube_disabled:
        youtube_note = "YouTube foi desativado pelo perfil de execução; a ausência de coleta não representa ausência de cobertura."
    elif youtube_unavailable:
        youtube_note = "A pesquisa web no YouTube ficou indisponível nesta execução; isso não representa ausência de cobertura na plataforma."
    elif youtube_status == "NOT_CONFIGURED":
        youtube_note = "YouTube não foi consultado porque OPENAI_API_KEY não está configurada."
    platforms = MediaScout.platform_status(llm_is_configured())
    for platform in platforms:
        if platform["platform"] != "YouTube":
            continue
        if youtube_disabled:
            platform["status"] = "desativado para este perfil"
        elif youtube_unavailable:
            platform["status"] = "indisponível nesta execução"
        elif youtube_status == "NOT_CONFIGURED":
            platform["status"] = "não configurado"

    settings = get_settings()
    scout_status = {
        "name": "Agente de monitoramento de veículos",
        "web_tasks": (
            min(len(MediaScout(project.topic, project.topic_profile).web_tasks()), settings.max_search_queries)
            if project else 0
        ),
        "youtube_tasks": (
            len(youtube_tasks_for_execution(project))
            if project else 0
        ),
        "platforms": platforms,
    }

    # Conflitos materiais da validação cruzada não entram nos rankings nem são
    # apresentados como cobertura auditável até que alguém os revise.
    youtube_candidates = db.scalars(
        select(MediaItem).where(
            MediaItem.project_id == project_id,
            MediaItem.status == "VALID",
        )
    ).all()
    raw_youtube_items = [item for item in youtube_candidates if is_youtube_host(item.domain or "")]
    youtube_conflicts = [item for item in raw_youtube_items if item.cross_validation_status == "CONFLICT"]
    youtube_items = [item for item in raw_youtube_items if item.cross_validation_status != "CONFLICT"]

    channels: dict[str, dict] = {}
    priority_channel_checks = []
    for label, _channel_name in PRIORITY_YOUTUBE_CHANNELS:
        matching = [
            item for item in youtube_items
            if matches_priority_youtube_channel(item.source_name, label)
        ]
        if matching:
            available_views = [item.view_count for item in matching if item.view_count is not None]
            views = sum(available_views) if available_views else None
            lead = max(matching, key=lambda item: item.view_count if item.view_count is not None else -1)
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
        elif youtube_disabled:
            priority_channel_checks.append(
                {"channel": label, "videos": 0, "views": None, "result": "coleta desativada para este perfil", "lead_url": ""}
            )
        elif youtube_unavailable or youtube_status == "NOT_CONFIGURED":
            priority_channel_checks.append(
                {"channel": label, "videos": 0, "views": None, "result": "coleta indisponível nesta execução", "lead_url": ""}
            )
        else:
            priority_channel_checks.append(
                {"channel": label, "videos": 0, "views": None, "result": "sem item validado na amostra", "lead_url": ""}
            )

    for item in youtube_items:
        channel = item.source_name or "Canal não identificado"
        current = channels.setdefault(
            channel,
            {
                "channel": channel,
                "videos": 0,
                "views": 0,
                "view_count_items": 0,
                "lead_title": item.title,
                "lead_url": item.url,
                "lead_views": item.view_count if item.view_count is not None else -1,
            },
        )
        current["videos"] += 1
        if item.view_count is not None:
            current["views"] += item.view_count
            current["view_count_items"] += 1
        item_views = item.view_count if item.view_count is not None else -1
        if item_views > current["lead_views"]:
            current.update(
                {"lead_title": item.title, "lead_url": item.url, "lead_views": item_views}
            )

    top_youtube_channels = [
        {
            **row,
            "views": row["views"] if row["view_count_items"] else None,
        }
        for row in sorted(
            channels.values(),
            key=lambda row: (row["views"] if row["view_count_items"] else -1, row["videos"]),
            reverse=True,
        )[:5]
    ]
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

    def content_platform(item: MediaItem) -> str:
        host = (item.domain or "").lower().split(":")[0]
        if is_youtube_host(host):
            return "YouTube"
        if host == "instagram.com" or host.endswith(".instagram.com"):
            return "Instagram"
        if host in {"x.com", "twitter.com"} or host.endswith(".x.com") or host.endswith(".twitter.com"):
            return "X"
        if host == "facebook.com" or host.endswith(".facebook.com"):
            return "Facebook"
        if host == "tiktok.com" or host.endswith(".tiktok.com"):
            return "TikTok"
        return "Site"

    # Ranking cross-plataforma somente quando existe uma métrica numérica de
    # alcance preservada no corpus. Isso evita atribuir alcance fictício a
    # matérias de sites que não expõem visualizações ao coletor.
    measurable_items = [
        item for item in youtube_candidates
        if item.view_count is not None and not _low_information_title(item.title)
    ]
    top_reach_contents = [
        {
            "source": source_label(item),
            "title": item.title,
            "platform": content_platform(item),
            "reach": item.view_count,
            "reach_metric": "visualizações",
            "published_at": (
                inferred_publication_date(item).isoformat()
                if inferred_publication_date(item)
                else None
            ),
            "url": item.url,
        }
        for item in sorted(
            measurable_items,
            key=lambda item: (
                item.view_count if item.view_count is not None else -1,
                inferred_publication_date(item) or date.min,
                item.id,
            ),
            reverse=True,
        )[:5]
    ]

    _, metric_flags = execution_flags(project) if project else ("MIDIATICO_SIMPLES", EXECUTION_PROFILE_DEFAULTS["MIDIATICO_SIMPLES"])
    facts = fact_events_for_main_report(db, project_id) if metric_flags["enable_fact_layer"] else []
    fact_validation = (
        fact_event_validation_summary(db, project_id)
        if metric_flags["enable_fact_layer"]
        else {"total": 0, "eligible": 0, "excluded": 0, "excluded_by_reason": {}}
    )
    return {
        "items_found": total,
        "valid_items": valid,
        "discarded_items": total - valid,
        "unique_vehicles": vehicles,
        "collection_days": collection_days,
        "isp_mentioned_items": isp,
        "isp_mention_percent": round((isp / valid * 100), 1) if valid else 0,
        "themes": [{"theme": row[0], "items": row[1]} for row in themes],
        "portal_checks": portal_checks,
        "media_scout": scout_status,
        "youtube_videos": len(youtube_items),
        "youtube_conflicts_excluded": len(youtube_conflicts),
        "youtube_collection_status": youtube_status,
        "youtube_collection_note": youtube_note,
        "youtube_collection_error": youtube_error,
        "youtube_priority_channel_checks": priority_channel_checks,
        "top_youtube_channels": top_youtube_channels,
        "top_youtube_videos": top_youtube_videos,
        "youtube_cross_validation": {
            "confirmed": sum(item.cross_validation_status == "CONFIRMED" for item in raw_youtube_items),
            "partial": sum(item.cross_validation_status == "PARTIALLY_CONFIRMED" for item in raw_youtube_items),
            "insufficient": sum(item.cross_validation_status == "INSUFFICIENT_EVIDENCE" for item in raw_youtube_items),
            "conflicts": len(youtube_conflicts),
        },
        "top_reach_contents": top_reach_contents,
        "top_reach_methodology": (
            "Ranking considera apenas itens validados com métrica numérica de alcance disponível no corpus; "
            "itens sem visualizações/alcance verificável não são ordenados como se tivessem alcance zero."
        ),
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
    execution_profile, flags = execution_flags(project)
    fact_events = fact_events_for_main_report(db, project.id) if flags["enable_fact_layer"] else []
    fact_evidence = (
        fact_assertions_for_report(db, project.id, main_report_only=True)
        if flags["enable_fact_layer"]
        else []
    )

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

    writer_instructions = (
        WRITER_PROMPT
        + " Estruture o texto nas seções fornecidas pelo schema. "
        + "A métrica isp_mentioned_items/isp_mention_percent mede somente MENÇÃO textual à instituição. "
        + "Nunca a descreva como protagonismo, liderança, destaque institucional ou centralidade editorial sem evidência específica. "
    )
    if flags["enable_fact_layer"]:
        writer_instructions += (
            "A tabela factual será renderizada diretamente pelo sistema; "
            "fact_layer_intro deve apenas contextualizá-la, sem reescrever ou alterar os fatos."
        )
    else:
        writer_instructions += (
            "Este perfil NÃO usa camada factual individual. Não introduza nomes de vítimas, listas nominais "
            "ou reconstruções de ocorrências individuais apenas para preencher essa seção. "
            "Use fact_layer_intro como uma frase curta indicando que a camada factual individual não integrou o escopo."
        )

    result = structured_response(
        instructions=writer_instructions,
        payload={
            "project": _project_payload(project, for_report=True),
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
        "project": _project_payload(project, for_report=True),
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
    payload["project"] = _project_payload(project, for_report=True)
    payload["metrics"] = metrics(db, project.id)
    payload["corpus"] = corpus_for_project(db, project.id)
    payload["traditional_corpus"], payload["social_corpus"] = split_corpus(payload["corpus"])
    _, flags = execution_flags(project)
    payload["fact_events"] = fact_events_for_main_report(db, project.id) if flags["enable_fact_layer"] else []
    payload["fact_evidence"] = (
        fact_assertions_for_report(db, project.id, main_report_only=True)
        if flags["enable_fact_layer"]
        else []
    )
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

    # Relatórios antigos, relatórios gerados por /ai/report ou execuções que
    # persistiram o texto antes do QA podem permanecer com status PENDING.
    # Ao solicitar o PDF final, executamos o QA uma vez antes de bloquear a
    # exportação. Isso evita exigir uma chamada manual ao endpoint /qa.
    if not allow_draft and saved.qa_status == "PENDING":
        qa_payload = _hydrate_cached_report(db, project, saved)
        run_report_qa(db, project, qa_payload)
        db.refresh(saved)

    if not allow_draft and saved.qa_status != "APPROVED":
        blocking = [
            finding
            for finding in (saved.qa_findings or [])
            if finding.get("severity") in {"CRITICAL", "HIGH"}
        ]
        detail = "; ".join(
            f"{finding.get('code', 'QA')}: {finding.get('message', '')}"
            for finding in blocking[:3]
        )
        suffix = f" Motivo(s): {detail}" if detail else ""
        raise RuntimeError(
            f"O relatório não passou pelo QA final (status: {saved.qa_status})."
            f"{suffix} Use export-draft.pdf apenas para revisão interna."
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
    """Executa somente os agentes necessários para o perfil selecionado."""

    def check() -> None:
        if cancel_check:
            cancel_check()

    def stage(key: str, status: str, detail: str | None = None) -> None:
        if progress_callback:
            progress_callback(key, status, detail)

    def detail_for(key: str) -> Callable[[str], None]:
        return lambda detail: stage(key, "RUNNING", detail)

    # 1. Perfil temático: necessário até no modo AUTO, pois decide o pipeline efetivo.
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
    profile_note = None
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        profile_state = project.topic_profile or {}
        product_status = str(profile_state.get("product_status") or "NOT_CONFIRMED")
        launch_confirmed = bool(profile_state.get("launch_date_confirmed"))

        if product_status == "PUBLISHED":
            trusted_launch = _trusted_launch_date(project)
            if launch_confirmed and trusted_launch:
                profile_note = (
                    f"produto publicado confirmado · lançamento real confirmado em "
                    f"{trusted_launch.isoformat()}"
                )
            else:
                profile_note = "produto publicado confirmado · data exata de lançamento não confirmada"
        elif product_status == "ANNOUNCED":
            expected = profile_state.get("expected_launch_date")
            profile_note = "produto anunciado; publicação efetiva ainda não confirmada"
            if expected:
                profile_note += f" · previsão localizada: {expected}"
        else:
            profile_note = "confirmação documental do produto pendente; seguindo por busca temática"

    execution_profile, flags = execution_flags(project)
    stage(
        "profile",
        "DONE",
        (
            f"Tema: {project.project_type} · perfil de execução: {execution_profile}"
            + (f" · {profile_note}" if profile_note else "")
        ),
    )

    # 2. Planejamento de buscas. plan_queries_with_llm já respeita enable_fact_layer.
    check()
    stage("search_plan", "RUNNING", "Gerando consultas apenas para as finalidades habilitadas")
    planned = plan_queries_with_llm(db, project)
    stage("search_plan", "DONE", f"{len(planned)} consulta(s) criada(s)")

    # 3. Coleta web + YouTube opcional.
    check()
    stage("collection", "RUNNING", "Consultando fontes web: Tavily primário com fallback automático para OpenAI Web Search")
    if flags["enable_youtube"]:
        stage("youtube", "RUNNING", "Pesquisando vídeos no YouTube: API oficial com fallback para OpenAI Web Search")
    else:
        stage("youtube", "SKIPPED", f"Desativado pelo perfil {execution_profile}")

    collection_agents = collect_media_agents(
        db,
        project,
        enable_youtube=flags["enable_youtube"],
        cancel_check=check,
        web_progress=detail_for("collection"),
        youtube_progress=detail_for("youtube") if flags["enable_youtube"] else None,
    )
    web = collection_agents["web"]
    collected = int(web["collected"])
    web_stats = web.get("stats") or {}
    if web["status"] == "COMPLETED":
        stage("collection", "DONE", f"{collected} novo(s) item(ns) coletado(s) via Tavily")
    elif web["status"] == "FALLBACK_WEB_SEARCH":
        switch_note = ""
        if int(web_stats.get("tavily_circuit_breaker_trips", 0)):
            switch_note = (
                f" Tavily desativado após {int(web_stats.get('tavily_attempts', 0))} tentativa(s); "
                f"circuit breaker aberto na consulta {int(web_stats.get('tavily_switched_at_query', 0))}."
            )
        stage(
            "collection",
            "DONE",
            f"{collected} novo(s) item(ns); OpenAI Web Search: "
            f"{int(web_stats.get('web_search_fallback_queries', 0))} consulta(s), "
            f"{int(web_stats.get('web_search_fallback_results', 0))} resultado(s) retornado(s), "
            f"{int(web_stats.get('web_search_fallback_added', 0))} aceito(s) e "
            f"{int(web_stats.get('web_search_fallback_rejected', 0))} rejeitado(s) pelo filtro temático."
            f"{switch_note}",
        )
    elif web["status"] == "PARTIAL":
        stage(
            "collection",
            "DONE",
            f"Coleta parcial: {collected} novo(s) item(ns); "
            f"fallback OpenAI aceitou {int(web_stats.get('web_search_fallback_added', 0))} item(ns); "
            f"{int(web_stats.get('failed_queries', 0))} consulta(s) não puderam ser concluídas",
        )
    else:
        stage(
            "collection",
            "FAILED",
            f"Coleta web indisponível nesta execução: {str(web.get('error') or '')[:180]}",
        )

    youtube = collection_agents["youtube"]
    youtube_collected = int(youtube["collected"])
    project.youtube_collection_status = str(youtube["status"])
    project.youtube_collection_error = youtube.get("error")
    if project.youtube_collection_status == "DISABLED":
        stage("youtube", "SKIPPED", f"Desativado pelo perfil {execution_profile}")
    elif project.youtube_collection_status == "FALLBACK_WEB_SEARCH":
        stage("youtube", "DONE", f"Fallback OpenAI Web Search concluído: {youtube_collected} vídeo(s)")
    elif project.youtube_collection_status == "UNAVAILABLE":
        stage(
            "youtube",
            "SKIPPED",
            "Coleta no YouTube indisponível; isso não será interpretado como ausência de cobertura.",
        )
    elif project.youtube_collection_status == "NOT_CONFIGURED":
        stage("youtube", "SKIPPED", "YouTube API e fallback OpenAI Web Search não configurados")
    else:
        stage("youtube", "DONE", f"{youtube_collected} vídeo(s) coletado(s)")
    db.commit()

    # 4. Validação cruzada só existe se YouTube estiver habilitado.
    check()
    if not flags["enable_cross_validation"]:
        stage("cross_validation", "SKIPPED", "Opcional e desativada nesta execução")
    else:
        stage("cross_validation", "RUNNING", "Comparando metadados coincidentes do Tavily e do YouTube")
        try:
            cross_validation = validate_tavily_youtube_metadata(
                db,
                project,
                cancel_check=check,
                progress_detail=detail_for("cross_validation"),
            )
        except RuntimeError as exc:
            stage("cross_validation", "SKIPPED", f"Validação cruzada indisponível: {str(exc)[:180]}")
        else:
            if cross_validation["skipped"]:
                if cross_validation.get("reason") == "NO_COMPARABLE_ITEMS":
                    stage(
                        "cross_validation",
                        "SKIPPED",
                        "Não necessária: nenhum vídeo foi obtido por dois coletores independentes",
                    )
                else:
                    stage("cross_validation", "SKIPPED", "OPENAI_API_KEY não configurada")
            else:
                stage("cross_validation", "DONE", f"{cross_validation['validated']} vídeo(s) comparado(s)")

    # Valores padrão permitem retornar um resultado estável mesmo quando a camada factual é pulada.
    fact_pass_1 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_1 = {"events": 0, "confirmed": 0, "partial": 0, "conflicts": 0}
    nominal_created = 0
    second_collected = 0
    fact_pass_2 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_2 = dict(fact_resolution_1)

    # 5. Camada factual opcional.
    fact_layer_active = flags["enable_fact_layer"] and project.project_type == "EVENT_TOPIC"
    if not fact_layer_active:
        reason = (
            f"Não necessária para o perfil {execution_profile}"
            if not flags["enable_fact_layer"]
            else f"Tipo de projeto {project.project_type} não exige fatos individuais"
        )
        for key in ("facts_pass_1", "fact_resolution_1", "nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(key, "SKIPPED", reason)
    else:
        check()
        stage("facts_pass_1", "RUNNING", "Extraindo datas, causas, locais e evidências por campo")
        fact_pass_1 = extract_project_facts(
            db,
            project,
            cancel_check=check,
            progress_detail=detail_for("facts_pass_1"),
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
        fact_resolution_2 = dict(fact_resolution_1)

        # Busca nominal é uma camada extra, reservada ao perfil completo ou override explícito.
        if not flags["enable_nominal_followup"]:
            stage("nominal_plan", "SKIPPED", f"Busca nominal desativada no perfil {execution_profile}")
            stage("nominal_collection", "SKIPPED", "Sem segunda coleta nominal")
            stage("facts_pass_2", "SKIPPED", "Sem segunda passagem factual")
            stage("fact_resolution_2", "SKIPPED", "A consolidação da primeira passagem foi mantida")
        else:
            check()
            stage("nominal_plan", "RUNNING", "Criando buscas somente para nomes já identificados")
            nominal_created = plan_nominal_followups(db, project)
            stage("nominal_plan", "DONE", f"{nominal_created} consulta(s) nominal(is) criada(s)")

            if nominal_created:
                check()
                stage("nominal_collection", "RUNNING", "Buscando corroboradores e fontes complementares por nome")
                second_collected = collect_tavily(
                    db,
                    project.id,
                    cancel_check=check,
                    progress_detail=detail_for("nominal_collection"),
                )
                stage("nominal_collection", "DONE", f"{second_collected} novo(s) item(ns) coletado(s)")

                check()
                stage("facts_pass_2", "RUNNING", "Extraindo evidências das novas fontes nominais")
                fact_pass_2 = extract_project_facts(
                    db,
                    project,
                    cancel_check=check,
                    progress_detail=detail_for("facts_pass_2"),
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

    # 6. Núcleo midiático: sempre executado.
    check()
    stage("validation", "RUNNING", "Validando janela de publicação e aderência temática")
    validation = validate_and_classify(
        db,
        project,
        cancel_check=check,
        progress_detail=detail_for("validation"),
    )
    stage(
        "validation",
        "DONE",
        f"{validation['valid']} válido(s); {validation['discarded']} descartado(s); "
        f"{validation.get('llm_calls', 0)} chamada(s) LLM em lote",
    )

    check()
    stage("classification", "RUNNING", "Classificando enquadramento, fidelidade e menção institucional")
    classification = classify_with_llm(
        db,
        project,
        cancel_check=check,
        progress_detail=detail_for("classification"),
    )
    classification_detail = (
        f"{classification.get('updated', 0)} item(ns) classificado(s); "
        f"{classification.get('llm_calls', 0)} chamada(s) LLM em lote"
    )
    if classification.get("failed_batches"):
        classification_detail += f"; {classification['failed_batches']} lote(s) com falha"
    stage("classification", "DONE", classification_detail)

    check()
    report_detail = (
        "Redigindo relatório com camada factual individual"
        if fact_layer_active
        else "Redigindo relatório de repercussão sem camada factual individual"
    )
    stage("report", "RUNNING", report_detail)
    drafted = draft_report_with_llm(db, project)
    stage("report", "DONE", "Relatório estruturado e persistido")

    check()
    stage("qa", "RUNNING", "Executando verificações determinísticas e auditoria final")
    qa = run_report_qa(db, project, drafted)
    stage("qa", "DONE", f"QA {qa['status']}")

    project.status = "REPORT_READY" if qa["approved"] else "REPORT_NEEDS_REVIEW"
    db.commit()

    return {
        "project": _project_payload(project, for_report=True),
        "profile": profile,
        "execution_profile": execution_profile,
        "execution_flags": flags,
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

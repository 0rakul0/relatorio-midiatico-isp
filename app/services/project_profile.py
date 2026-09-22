from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.models import OfficialFact, Project
from app.schemas import InstitutionalProductProfileResponse
from app.source_registry import OFFICIAL_SECURITY_SOURCES
from app.topic_profile import build_topic_profile, normalized_text
from app.tools import build_agent_tools
from app.services.execution_profile import execution_flags
from app.services.collection.common import result_publication_date


PROJECT_TYPE_LABELS = {
    "INSTITUTIONAL_PRODUCT": "Produto institucional",
    "EVENT_TOPIC": "Tema factual / evento",
    "GENERAL_TOPIC": "Tema geral",
    "AUTO": "Classificação automática",
}

def _host(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def trusted_launch_date(project: Project) -> date | None:
    options = project.execution_options or {}
    profile = project.topic_profile or {}
    trusted = bool(options.get("launch_date_user_supplied") or profile.get("launch_date_confirmed"))
    return project.launch_date if trusted else None


def project_payload(project: Project, *, for_report: bool = False) -> dict:
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
    trusted_launch = trusted_launch_date(project)

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
        "collection_window_source": (project.execution_options or {}).get("collection_window_source"),
        "event_window_source": (project.execution_options or {}).get("event_window_source"),
    }


def discover_project_profile(db: Session, project: Project) -> dict:
    """Resolve o perfil e usa o agente único para confirmação documental.

    Para produto institucional, a busca externa NÃO é executada antes do agente.
    O ReportAgent recebe a tool ``pesquisar_internet`` e decide se precisa
    chamá-la. Isso mantém a separação: agente decide; tool executa.
    """
    profile = build_topic_profile(project.topic)
    project.project_type = profile["project_type"]
    project.topic_profile = profile

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

    if not llm_is_configured():
        profile_state = dict(project.topic_profile or {})
        profile_state.update(
            {
                "product_status": "NOT_CONFIRMED",
                "product_confirmed": False,
                "product_published": False,
                "launch_date_confirmed": False,
                "launch_status": "NOT_FOUND",
                "profile_discovery_stats": {
                    "agent_available": False,
                    "agent_optional_search_calls": 0,
                    "agent_optional_search_results": 0,
                },
            }
        )
        project.topic_profile = profile_state
        project.status = "PROFILE_NEEDS_REVIEW"
        db.commit()
        return {
            "status": project.status,
            "project_type": project.project_type,
            "sources": 0,
            "facts": 0,
            "product_confirmed": False,
            "product_published": False,
            "launch_date_confirmed": False,
            "launch_required": False,
            "warning": "LLM não configurada para confirmação documental do produto",
        }

    settings = get_settings()
    product_name = str(profile.get("product_name") or project.topic or "").strip()
    product_anchor = str(profile.get("product_anchor") or product_name).strip()
    sources: list[dict] = []
    seen: set[str] = set()
    discovery_stats = {
        "agent_available": True,
        "agent_optional_search_calls": 0,
        "agent_optional_search_results": 0,
        "providers": {},
    }

    official_domains = [
        source["domain"]
        for source in OFFICIAL_SECURITY_SOURCES
        if source.get("label") in {"ISP", "ISP Conecta", "Governo do RJ"}
    ]
    anchor_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", normalized_text(f"{product_name} {product_anchor}"))
        if len(token) >= 4
    }

    def _documentalist_accepts(row: dict) -> bool:
        host = _host(str(row.get("url") or ""))
        if any(host == domain.lower().split("/")[0] or host.endswith("." + domain.lower().split("/")[0]) for domain in official_domains):
            return True
        text = normalized_text(
            f"{row.get('title') or ''} {row.get('snippet') or ''} {row.get('content') or ''}"
        )
        return any(token in text for token in anchor_tokens)

    def _documentalist_source_sink(
        rows: list[dict],
        provider: str,
        query: str,
    ) -> list[dict]:
        normalized: list[dict] = []
        provider_counts = discovery_stats["providers"]
        provider_counts[provider] = int(provider_counts.get(provider, 0)) + 1

        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            if not _documentalist_accepts(row):
                continue

            if url in seen:
                existing_index = next(
                    (idx for idx, source in enumerate(sources) if source.get("url") == url),
                    None,
                )
                item = dict(row)
                item["source_index"] = existing_index
                normalized.append(item)
                continue

            seen.add(url)
            content = str(row.get("content") or row.get("snippet") or "")[:3500]
            sources.append(
                {
                    "title": row.get("title") or "Sem titulo",
                    "url": url,
                    "content": content,
                    "search_provider": row.get("provider") or provider,
                    "search_query": query,
                }
            )
            item = dict(row)
            item["source_index"] = len(sources) - 1
            normalized.append(item)

        discovery_stats["agent_optional_search_calls"] += 1
        discovery_stats["agent_optional_search_results"] += len(normalized)
        return normalized

    tools = build_agent_tools(enable_web=True, web_sink=_documentalist_source_sink)
    result = get_report_agent().run(
        task="documentalist",
        payload={
            "topic": project.topic,
            "product_name": product_name,
            "product_anchor": product_anchor,
            "product_search_variants": profile.get("product_search_variants") or [],
            "official_domains": official_domains,
            "sources": [],
        },
        schema_name="institutional_product_profile_v2",
        response_model=InstitutionalProductProfileResponse,
        tools=tools,
        max_output_tokens=5500,
        max_tool_rounds=settings.max_agent_tool_rounds,
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

    if launch_confirmed:
        product_status = "PUBLISHED"

    product_confirmed = product_status in {"PUBLISHED", "ANNOUNCED"}
    product_published = product_status == "PUBLISHED"

    options = project.execution_options or {}
    if launch_confirmed and parsed_launch_date and not options.get("launch_date_user_supplied"):
        project.launch_date = parsed_launch_date

    facts_added = 0
    for fact in result.get("official_facts", []):
        source_index = fact.get("source_index")
        if not isinstance(source_index, int) or source_index < 0 or source_index >= len(sources):
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
                period_start=result_publication_date(fact.get("period_start")),
                period_end=result_publication_date(fact.get("period_end")),
                unit=fact.get("unit"),
            )
        )
        facts_added += 1

    expected_launch_date = None
    if result.get("expected_launch_date"):
        try:
            expected_launch_date = date.fromisoformat(
                str(result["expected_launch_date"])[:10]
            ).isoformat()
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
    project.status = "PROFILED" if product_published else "PROFILE_NEEDS_REVIEW"
    db.commit()

    warning = None
    if product_published and not launch_confirmed:
        warning = "Produto publicado confirmado; data exata de lançamento não localizada"
    elif product_status == "ANNOUNCED":
        warning = "Produto anunciado, mas publicação efetiva ainda não confirmada"
    elif not product_confirmed:
        warning = "Produto institucional identificado, mas publicação não confirmada documentalmente"

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



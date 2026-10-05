from __future__ import annotations

from collections import Counter
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.fact_layer import fact_event_validation_summary, fact_events_for_main_report, operation_events_for_report, operation_inventory_summary
from app.media_scout import MediaScout
from app.models import MediaItem, Project, SearchQuery
from app.source_registry import PRIORITY_PORTALS, PRIORITY_YOUTUBE_CHANNELS
from app.services.execution_profile import EXECUTION_PROFILE_DEFAULTS, execution_flags
from app.services.collection.common import inferred_publication_date, publication_year, source_label
from app.services.collection.media_origin import (
    PORTAL_NOTICIAS,
    REDE_SOCIAL,
    YOUTUBE,
    classify_media_origin,
)
from app.services.collection.youtube_helpers import is_youtube_host, matches_priority_youtube_channel
from app.services.validation import low_information_title
from app.metrics.corpus import (
    corpus_domain as _corpus_domain,
    corpus_for_project,
    corpus_item_quality as _corpus_item_quality,
    corpus_year as _corpus_year,
    deduplicate_corpus,
    normalized_report_title as _normalized_report_title,
    report_title_tokens as _report_title_tokens,
    report_url_key as _report_url_key,
    same_report_item as _same_report_item,
    split_corpus,
    split_corpus_by_origin,
)


def metrics(db: Session, project_id: int) -> dict:
    total = db.scalar(select(func.count(MediaItem.id)).where(MediaItem.project_id == project_id)) or 0
    raw_valid = db.scalar(
        select(func.count(MediaItem.id)).where(
            MediaItem.project_id == project_id,
            MediaItem.status == "VALID",
        )
    ) or 0

    report_corpus = corpus_for_project(db, project_id)
    valid = len(report_corpus)
    duplicates_collapsed = max(0, int(raw_valid) - valid)

    domain_values = {
        _corpus_domain(item)
        for item in report_corpus
        if _corpus_domain(item)
    }
    vehicles = len(domain_values)
    isp = sum(bool(item.get("isp_mentioned")) for item in report_corpus)

    theme_counts = Counter(
        str(item.get("theme"))
        for item in report_corpus
        if item.get("theme")
    )
    themes = sorted(
        theme_counts.items(),
        key=lambda row: (-row[1], row[0].casefold()),
    )

    media_origin_counts = {
        PORTAL_NOTICIAS: 0,
        REDE_SOCIAL: 0,
        YOUTUBE: 0,
    }
    for item in report_corpus:
        origin = item.get("media_origin")
        if origin not in media_origin_counts:
            origin = classify_media_origin(item.get("url"), item.get("domain"))
        media_origin_counts[origin] = media_origin_counts.get(origin, 0) + 1

    domains = [
        _corpus_domain(item)
        for item in report_corpus
        if _corpus_domain(item)
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
        youtube_note = "YouTube não foi consultado porque nenhum coletor disponível conseguiu executar a busca."
    platforms = MediaScout.platform_status(not youtube_disabled and not youtube_unavailable)
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
        # YouTube é roteado dos resultados do DuckDuckGo; não há mais um
        # planejador de consultas independente para a plataforma.
        "youtube_tasks": 0,
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
            "channel": row["channel"],
            "videos": row["videos"],
            "views": row["views"] if row["view_count_items"] else None,
            "lead_title": row["lead_title"],
            "lead_url": row["lead_url"],
            "lead_views": row["lead_views"] if row["lead_views"] >= 0 else None,
        }
        for row in sorted(
            channels.values(),
            key=lambda row: (row["views"] if row["view_count_items"] else -1, row["videos"]),
            reverse=True,
        )
    ]
    top_youtube_videos = [
        {
            "title": item.title,
            "channel": item.source_name or "Canal não identificado",
            "views": item.view_count,
            "published_year": publication_year(item),
            "url": item.url,
        }
        for item in sorted(youtube_items, key=lambda item: item.view_count or 0, reverse=True)
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
        if item.view_count is not None and not low_information_title(item.title)
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
        )
    ]

    _, metric_flags = execution_flags(project) if project else ("MIDIATICO_SIMPLES", EXECUTION_PROFILE_DEFAULTS["MIDIATICO_SIMPLES"])
    operation_inventory = (
        operation_inventory_summary(db, project)
        if project and metric_flags["enable_fact_layer"]
        else {
            "operations": 0,
            "months_expected": 0,
            "months_with_operations": [],
            "months_with_official_support": [],
            "months_missing_operations": [],
            "months_missing_official_support": [],
            "coverage_ratio": None,
            "official_coverage_ratio": None,
        }
    )
    operation_events = operation_events_for_report(db, project_id) if project and metric_flags["enable_fact_layer"] else []
    facts = fact_events_for_main_report(db, project_id) if metric_flags["enable_fact_layer"] else []
    fact_validation = (
        fact_event_validation_summary(db, project_id)
        if metric_flags["enable_fact_layer"]
        else {"total": 0, "eligible": 0, "excluded": 0, "excluded_by_reason": {}}
    )
    return {
        "items_found": total,
        "valid_items": valid,
        "raw_valid_items": int(raw_valid),
        "duplicates_collapsed": duplicates_collapsed,
        "discarded_items": total - int(raw_valid),
        "unique_vehicles": vehicles,
        "media_origin_counts": media_origin_counts,
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
        "operation_inventory": operation_inventory,
        "operation_events": operation_events,
        "facts": {
            "events": len(facts),
            "confirmed": sum(item["resolution_status"] == "CONFIRMED" for item in facts),
            "partial": sum(item["resolution_status"] == "PARTIALLY_CONFIRMED" for item in facts),
            "conflicts": sum(item["resolution_status"] == "SOURCE_CONFLICT" for item in facts),
            "excluded_from_main_report": fact_validation["excluded"],
            "exclusion_reasons": fact_validation["excluded_by_reason"],
        },
    }



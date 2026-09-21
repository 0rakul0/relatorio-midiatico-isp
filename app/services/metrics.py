from __future__ import annotations

from collections import Counter
from datetime import date
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.fact_layer import fact_event_validation_summary, fact_events_for_main_report
from app.media_scout import MediaScout
from app.models import Classification, MediaItem, Project, SearchQuery
from app.source_registry import PRIORITY_PORTALS, PRIORITY_YOUTUBE_CHANNELS
from app.services.execution_profile import EXECUTION_PROFILE_DEFAULTS, execution_flags
from app.services.collection.common import inferred_publication_date, publication_year, source_label
from app.services.collection.media_origin import (
    PORTAL_NOTICIAS,
    REDE_SOCIAL,
    YOUTUBE,
    classify_media_origin,
)
from app.services.collection.youtube_helpers import is_youtube_host, matches_priority_youtube_channel, youtube_tasks_for_execution
from app.services.validation import low_information_title


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "mkt_tok", "vero_id",
}
_TITLE_STOPWORDS = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "na", "nas", "no", "nos", "o", "os", "para", "por", "um", "uma",
}
_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)


def _report_url_key(value: str | None) -> str:
    """Normaliza URL para detectar aliases de rastreamento no corpus final."""
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold()

    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    kept_query = []
    for key, value_part in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_QUERY_KEYS:
            continue
        kept_query.append((key, value_part))
    kept_query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))

    return urlunsplit(
        (
            parsed.scheme.casefold() or "https",
            netloc,
            path,
            urlencode(kept_query, doseq=True),
            "",
        )
    )


def _normalized_report_title(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").casefold())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return " ".join(_TITLE_TOKEN_RE.findall(normalized))


def _report_title_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in _normalized_report_title(value).split()
        if token not in _TITLE_STOPWORDS
    }


def _corpus_domain(item: dict) -> str:
    domain = str(item.get("domain") or "").casefold().strip()
    if not domain and item.get("url"):
        try:
            domain = (urlsplit(str(item["url"])).hostname or "").casefold()
        except ValueError:
            domain = ""
    return domain[4:] if domain.startswith("www.") else domain


def _corpus_year(item: dict) -> str:
    published_at = str(item.get("published_at") or "")
    if len(published_at) >= 4 and published_at[:4].isdigit():
        return published_at[:4]
    year = str(item.get("published_year") or "")
    return year if len(year) == 4 and year.isdigit() else ""


def _same_report_item(left: dict, right: dict) -> bool:
    left_url = _report_url_key(
        str(left.get("canonical_url") or left.get("url") or "")
    )
    right_url = _report_url_key(
        str(right.get("canonical_url") or right.get("url") or "")
    )
    if left_url and right_url and left_url == right_url:
        return True

    if _corpus_domain(left) != _corpus_domain(right):
        return False

    left_origin = left.get("media_origin")
    right_origin = right.get("media_origin")
    if left_origin and right_origin and left_origin != right_origin:
        return False

    left_year = _corpus_year(left)
    right_year = _corpus_year(right)
    if left_year and right_year and left_year != right_year:
        return False

    left_title = _normalized_report_title(left.get("title"))
    right_title = _normalized_report_title(right.get("title"))
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True

    left_tokens = _report_title_tokens(left.get("title"))
    right_tokens = _report_title_tokens(right.get("title"))
    if min(len(left_tokens), len(right_tokens)) < 3:
        return False

    intersection = len(left_tokens & right_tokens)
    containment = intersection / min(len(left_tokens), len(right_tokens))
    union = len(left_tokens | right_tokens)
    jaccard = intersection / union if union else 0.0
    shorter = min(left_title, right_title, key=len)
    title_containment = (
        len(shorter) >= 12
        and (left_title in right_title or right_title in left_title)
    )
    return containment >= 0.90 and (jaccard >= 0.72 or title_containment)


def _corpus_item_quality(item: dict) -> tuple[int, int, int, int]:
    return (
        1 if item.get("published_at") else 0,
        1 if item.get("evidence") or item.get("relation_evidence") else 0,
        len(str(item.get("title") or "")),
        1 if item.get("url") else 0,
    )


def deduplicate_corpus(corpus: list[dict]) -> list[dict]:
    """Colapsa aliases/duplicatas para apresentacao, preservando rastreabilidade."""
    representatives: list[dict] = []

    for source_item in corpus or []:
        candidate = dict(source_item)
        candidate["duplicate_count"] = int(candidate.get("duplicate_count") or 0)
        candidate["duplicate_urls"] = list(candidate.get("duplicate_urls") or [])

        duplicate_index = next(
            (
                index
                for index, existing in enumerate(representatives)
                if _same_report_item(existing, candidate)
            ),
            None,
        )
        if duplicate_index is None:
            representatives.append(candidate)
            continue

        existing = representatives[duplicate_index]
        duplicate_count = (
            int(existing.get("duplicate_count") or 0)
            + int(candidate.get("duplicate_count") or 0)
            + 1
        )

        all_urls: list[str] = []
        for value in [
            existing.get("url"),
            *(existing.get("duplicate_urls") or []),
            candidate.get("url"),
            *(candidate.get("duplicate_urls") or []),
        ]:
            url = str(value or "").strip()
            if url and url not in all_urls:
                all_urls.append(url)

        representative = (
            candidate
            if _corpus_item_quality(candidate) > _corpus_item_quality(existing)
            else existing
        )
        representative = dict(representative)
        representative["duplicate_count"] = duplicate_count
        representative["duplicate_urls"] = [
            url for url in all_urls if url != representative.get("url")
        ]
        representatives[duplicate_index] = representative

    return representatives


def corpus_for_project(db: Session, project_id: int) -> list[dict]:
    rows = db.execute(
        select(MediaItem, Classification)
        .outerjoin(Classification, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.desc().nullslast(), MediaItem.id.desc())
    ).all()
    raw_corpus = [
        {
            "title": item.title,
            "url": item.url,
            "canonical_url": item.canonical_url,
            "domain": item.domain,
            "media_origin": item.media_origin or classify_media_origin(item.url, item.domain),
            "theme": classification.theme if classification else None,
            "framing": classification.framing if classification else None,
            "evidence": classification.evidence if classification else item.relevance_evidence,
            "relation_type": item.relation_type,
            "relation_evidence": item.relevance_evidence,
            "corpus_origin": item.corpus_origin or "SEARCH",
            "search_source": item.search_source,
            "isp_mentioned": classification.isp_mentioned if classification else False,
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "published_year": publication_year(item),
            "source": source_label(item),
            "view_count": item.view_count,
        }
        for item, classification in rows
    ]
    return deduplicate_corpus(raw_corpus)

def split_corpus(corpus: list[dict]) -> tuple[list[dict], list[dict]]:
    traditional: list[dict] = []
    social: list[dict] = []
    for item in corpus:
        origin = item.get("media_origin")
        if not origin:
            domain = (item.get("domain") or "").lower()
            origin = classify_media_origin(None, domain)
        target = traditional if origin == PORTAL_NOTICIAS else social
        target.append(item)
    return traditional, social


def split_corpus_by_origin(corpus: list[dict]) -> dict[str, list[dict]]:
    """Separa o corpus em portal de notícias, redes sociais e YouTube."""
    buckets: dict[str, list[dict]] = {
        PORTAL_NOTICIAS: [],
        REDE_SOCIAL: [],
        YOUTUBE: [],
    }
    for item in corpus:
        origin = item.get("media_origin")
        if origin not in buckets:
            domain = (item.get("domain") or "").lower()
            origin = classify_media_origin(item.get("url"), domain)
        buckets[origin].append(item)
    return {
        "portal_noticias": buckets[PORTAL_NOTICIAS],
        "redes_sociais": buckets[REDE_SOCIAL],
        "youtube": buckets[YOUTUBE],
    }


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
        "facts": {
            "events": len(facts),
            "confirmed": sum(item["resolution_status"] == "CONFIRMED" for item in facts),
            "partial": sum(item["resolution_status"] == "PARTIALLY_CONFIRMED" for item in facts),
            "conflicts": sum(item["resolution_status"] == "SOURCE_CONFLICT" for item in facts),
            "excluded_from_main_report": fact_validation["excluded"],
            "exclusion_reasons": fact_validation["excluded_by_reason"],
        },
    }



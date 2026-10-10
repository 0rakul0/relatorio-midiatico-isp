from __future__ import annotations

from collections import defaultdict
import re

from app.topic_profile import normalized_text

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MediaItem, Project, SearchHit
from app.social.urls import platform_for_url, url_key


ELECTORAL_TERMS = (
    "eleicoes", "eleicao", "presidencia", "presidencial", "intencao de voto",
    "corrida eleitoral", "candidato", "candidatos", "polarizacao politica",
    "datafolha", "quaest", "atlasintel", "bolsonaro", "lula",
)
GENDER_VIOLENCE_TERMS = (
    "violencia contra mulher", "violencia contra as mulher",
    "violencia contra mulheres", "violencia de genero",
    "violencia domestica", "violencia familiar", "feminicidio",
    "agressao contra mulher", "assedi", "medida protetiva",
    "violencia politica contra mulher", "violencia politica de genero",
)


def social_topic_relevance(project: Project, title: str | None, snippet: str | None = None) -> tuple[bool, str]:
    """Guarda conservadora para impedir que posts eleitorais contaminem
    pesquisas sobre violencia contra mulheres. Nao apaga o item bruto.
    """
    topic = normalized_text(project.topic or "")
    body = normalized_text(" ".join(filter(None, [title, snippet])))
    is_gender_violence = (
        ("mulher" in topic and "violencia" in topic)
        or "feminicidio" in topic
    )
    if not is_gender_violence:
        return True, "OUT_OF_SCOPE_OF_SPECIAL_GUARD"
    if any(term in body for term in GENDER_VIOLENCE_TERMS):
        return True, "GENDER_VIOLENCE_EVIDENCE"
    if any(term in body for term in ELECTORAL_TERMS):
        return False, "ELECTORAL_CONTENT_WITHOUT_GENDER_VIOLENCE_LINK"
    # A ausencia de evidencia no titulo nao deve inventar relevancia;
    # posts curtos ou ambiguos ficam pendentes de verificacao.
    return False, "NO_VERIFIABLE_GENDER_VIOLENCE_LINK"


def has_duckduckgo_provenance(item: MediaItem) -> bool:
    source = str(item.search_source or "").strip().lower()
    if source.startswith("duckduckgo"):
        return True
    for entry in item.source_provenance or []:
        if not isinstance(entry, dict):
            continue
        provider = str(
            entry.get("source") or entry.get("provider") or ""
        ).strip().lower()
        if provider.startswith("duckduckgo"):
            return True
    return False


def candidate_urls(
    db: Session,
    project_id: int,
) -> dict[str, list[tuple[str, int | None, str | None]]]:
    found: dict[str, dict[str, tuple[str, int | None, str | None]]] = defaultdict(dict)

    for item in db.scalars(
        select(MediaItem).where(MediaItem.project_id == project_id)
    ).all():
        if not has_duckduckgo_provenance(item):
            continue
        project = db.get(Project, project_id)
        if project is not None and not social_topic_relevance(project, item.title, item.snippet)[0]:
            continue
        platform = platform_for_url(item.url)
        if not platform:
            continue
        found[platform][url_key(item.url)] = (item.url, item.id, item.title)

    for hit in db.scalars(
        select(SearchHit).where(SearchHit.project_id == project_id)
    ).all():
        if not str(hit.provider or "").strip().lower().startswith("duckduckgo"):
            continue
        project = db.get(Project, project_id)
        if project is not None and not social_topic_relevance(project, hit.title, hit.snippet)[0]:
            continue
        platform = platform_for_url(hit.url)
        if not platform:
            continue
        url = str(hit.url or "").strip()
        found[platform].setdefault(
            url_key(url), (url, hit.media_item_id, hit.title)
        )

    limit = max(1, int(get_settings().apify_social_max_posts_per_platform))
    return {
        platform: list(rows.values())[:limit]
        for platform, rows in found.items()
        if rows
    }


def social_discovery_terms(project: Project) -> list[str]:
    profile = project.topic_profile or {}
    values: list[str] = []

    # Consultas partem do tema completo. Evitar atores/acoes isoladas que
    # podem gerar resultados eleitorais sem qualquer elo com a pauta.
    values.append(project.topic)
    for key in ("event_search_variants", "product_search_variants", "search_synonyms", "subject_terms"):
        for value in profile.get(key) or []:
            text = " ".join(str(value).split()).strip()
            if text and social_topic_relevance(project, text)[0]:
                values.append(text)

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" ?.,;:")
        if len(text) < 3:
            continue
        lowered = text.casefold()
        for prefix in (
            "como está ",
            "como esta ",
            "como ficou ",
            "qual é ",
            "qual e ",
            "quais são ",
            "quais sao ",
        ):
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                lowered = text.casefold()
                break
        words = text.split()
        if len(words) > 8:
            text = " ".join(words[:8])
        key = text.casefold()
        if len(text) < 3 or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned


def social_recovery_terms(
    project: Project,
    primary_terms: list[str],
) -> list[str]:
    profile = project.topic_profile or {}
    values: list[str] = []

    values.append(project.topic)
    for term in primary_terms:
        if social_topic_relevance(project, term)[0]:
            values.append(term)

    year = str(project.collection_end.year) if project.collection_end else ""
    location = ""

    cleaned: list[str] = []
    seen = {item.casefold() for item in primary_terms}
    for value in values:
        text = " ".join(str(value or "").split()).strip(" ?.,;:")
        if len(text) < 3:
            continue
        words = text.split()
        if len(words) > 5:
            text = " ".join(words[:5])
        if (
            location
            and location.casefold() not in text.casefold()
            and len(text.split()) <= 3
        ):
            text = f"{text} {location}".strip()
        if year and year not in text and len(text.split()) <= 4:
            text = f"{text} {year}".strip()
        key = text.casefold()
        if not social_topic_relevance(project, text)[0] or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    return cleaned

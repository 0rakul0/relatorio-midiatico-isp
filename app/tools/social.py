"""Ferramenta deterministica de coleta social.

A decisao de habilitar esta capacidade pertence ao planejador. Este modulo
isola configuracao/contrato dos Actors do Apify para que services nao importem
providers diretamente.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.config import get_settings
from app.tools.providers.apify_social import ApifyUnavailable, run_actor_dataset
from app.tools.providers.duckduckgo import (
    DuckDuckGoUnavailable,
    search_text as duckduckgo_text,
)


class SocialCollectionUnavailable(RuntimeError):
    """Coleta social nao configurada ou temporariamente indisponivel."""


def _actor_for_platform(platform: str) -> str | None:
    settings = get_settings()
    return {
        "instagram": settings.apify_instagram_comments_actor_id,
        "facebook": settings.apify_facebook_comments_actor_id,
        "tiktok": settings.apify_tiktok_comments_actor_id,
        "x": settings.apify_x_comments_actor_id,
    }.get(platform)


def _actor_input(platform: str, urls: list[str], limit: int) -> dict:
    if platform == "instagram":
        return {
            "directUrls": urls,
            "resultsLimit": limit,
            "includeNestedComments": False,
        }
    if platform == "facebook":
        return {
            "startUrls": [{"url": url} for url in urls],
            "resultsLimit": limit,
            "includeNestedComments": False,
        }
    if platform == "tiktok":
        return {
            "postURLs": urls,
            "commentsPerPost": limit,
            "maxRepliesPerComment": 0,
            "resultsPerPage": min(100, limit),
        }
    if platform == "x":
        return {
            "postUrls": urls,
            "resultsLimit": limit,
            "includeOriginalPost": False,
        }
    raise SocialCollectionUnavailable(
        f"Plataforma social nao suportada: {platform}"
    )


def collect_public_comments(
    *,
    platform: str,
    urls: list[str],
    comments_per_post: int,
) -> tuple[str, list[dict]]:
    settings = get_settings()
    if not settings.apify_social_enabled:
        raise SocialCollectionUnavailable("Coleta social desativada na configuracao")
    if not settings.apify_api_token:
        raise SocialCollectionUnavailable("APIFY_API_TOKEN nao configurado")

    actor_id = _actor_for_platform(platform)
    if not actor_id:
        raise SocialCollectionUnavailable(
            f"Actor do Apify para {platform} nao configurado"
        )

    urls = [str(url).strip() for url in urls if str(url).strip()]
    if not urls:
        return actor_id, []

    limit = max(1, int(comments_per_post))
    try:
        rows = run_actor_dataset(
            actor_id=actor_id,
            token=settings.apify_api_token,
            payload=_actor_input(platform, urls, limit),
            base_url=settings.apify_api_base_url,
            timeout_seconds=settings.apify_social_timeout_seconds,
            max_items=len(urls) * limit,
        )
    except ApifyUnavailable as exc:
        raise SocialCollectionUnavailable(str(exc)) from exc
    return actor_id, rows



_SOCIAL_DISCOVERY_DOMAINS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com",),
    "tiktok": ("tiktok.com",),
    "x": ("x.com", "twitter.com"),
}


def discover_public_posts(
    *,
    terms: list[str],
    max_queries_per_platform: int = 2,
    results_per_query: int = 8,
) -> dict[str, list[dict]]:
    """Descobre URLs públicas de posts sociais via DuckDuckGo Text.

    O Apify continua responsável pelo enriquecimento/coleta de comentários.
    A descoberta fica independente dos Actors pagos e usa consultas site:
    específicas para cada plataforma.
    """
    settings = get_settings()
    cleaned_terms = list(dict.fromkeys(
        " ".join(str(term or "").split()).strip()
        for term in terms
        if " ".join(str(term or "").split()).strip()
    ))
    cleaned_terms = cleaned_terms[: max(1, int(max_queries_per_platform))]

    found: dict[str, dict[str, dict]] = {
        platform: {} for platform in _SOCIAL_DISCOVERY_DOMAINS
    }
    if not cleaned_terms:
        return {platform: [] for platform in _SOCIAL_DISCOVERY_DOMAINS}

    for platform, domains in _SOCIAL_DISCOVERY_DOMAINS.items():
        for term in cleaned_terms:
            for domain in domains:
                query = f'site:{domain} "{term}"'
                try:
                    rows = duckduckgo_text(
                        query,
                        max_results=max(1, int(results_per_query)),
                        region=settings.duckduckgo_region,
                        safesearch=settings.duckduckgo_safesearch,
                        retries=settings.duckduckgo_max_retries,
                        retry_base_seconds=settings.duckduckgo_retry_base_seconds,
                    )
                except DuckDuckGoUnavailable:
                    continue

                for row in rows:
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    host = urlparse(url).netloc.lower().split(":", 1)[0]
                    if host.startswith("www."):
                        host = host[4:]
                    if not any(host == base or host.endswith("." + base) for base in domains):
                        continue
                    item = dict(row)
                    item["discovery_query"] = query
                    item["social_platform"] = platform
                    item["provider"] = "duckduckgo_social"
                    found[platform].setdefault(url.rstrip("/").lower(), item)

    return {
        platform: list(rows.values())
        for platform, rows in found.items()
    }

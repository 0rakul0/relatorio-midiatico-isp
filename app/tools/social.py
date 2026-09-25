"""Ferramenta deterministica de coleta social.

A decisao de habilitar esta capacidade pertence ao planejador. Este modulo
isola configuracao/contrato dos Actors do Apify para que services nao importem
providers diretamente.
"""

from __future__ import annotations

from app.config import get_settings
from app.tools.providers.apify_social import ApifyUnavailable, run_actor_dataset


class SocialCollectionUnavailable(RuntimeError):
    """Coleta social nao configurada ou temporariamente indisponivel."""


def _actor_for_platform(platform: str) -> str | None:
    settings = get_settings()
    return {
        "instagram": settings.apify_instagram_comments_actor_id,
        "facebook": settings.apify_facebook_comments_actor_id,
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
    if platform == "x":
        return {
            "urls": urls,
            "category": "replies",
            "resultsPerCategory": limit,
            "scrapeAll": True,
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

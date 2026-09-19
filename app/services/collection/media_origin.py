"""Classificação determinística da origem de cada link capturado.

Todo URL preservado na coleta recebe um ``media_origin`` em
``SearchHit``/``MediaItem``:

- ``YOUTUBE``: vídeos do YouTube (youtube.com / youtu.be);
- ``REDE_SOCIAL``: posts/perfis de redes sociais;
- ``PORTAL_NOTICIAS``: todo o restante (portais de notícia, blogs,
  sites institucionais e demais páginas web).

A regra é propositalmente conservadora: só é rede social o que casa com
um domínio conhecido; o resto é portal. A classificação nunca descarta o
item, apenas organiza o corpus para análise e métricas.
"""

from __future__ import annotations

from urllib.parse import urlparse

YOUTUBE = "YOUTUBE"
REDE_SOCIAL = "REDE_SOCIAL"
PORTAL_NOTICIAS = "PORTAL_NOTICIAS"

MEDIA_ORIGINS = (PORTAL_NOTICIAS, REDE_SOCIAL, YOUTUBE)

SOCIAL_DOMAINS = (
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "threads.net",
    "linkedin.com",
    "pinterest.com",
    "pin.it",
    "reddit.com",
    "t.me",
    "telegram.me",
    "whatsapp.com",
    "wa.me",
    "kwai.com",
    "kuaishou.com",
    "bsky.app",
    "mastodon.social",
    "threads.com",
)


def _host_of(url: str | None, domain: str | None = None) -> str:
    if domain:
        return domain.lower().split(":")[0].strip().lstrip(".")
    if not url:
        return ""
    try:
        return (urlparse(url).netloc or "").lower().split(":")[0].strip().lstrip(".")
    except ValueError:
        return ""


def is_social_host(host: str) -> bool:
    host = (host or "").lower().split(":")[0].strip().lstrip(".")
    if not host:
        return False
    return any(host == base or host.endswith("." + base) for base in SOCIAL_DOMAINS)


def classify_media_origin(url: str | None, domain: str | None = None) -> str:
    """Devolve YOUTUBE, REDE_SOCIAL ou PORTAL_NOTICIAS para um link."""
    from app.services.collection.youtube_helpers import is_youtube_host

    host = _host_of(url, domain)
    if host and is_youtube_host(host):
        return YOUTUBE
    if is_social_host(host):
        return REDE_SOCIAL
    return PORTAL_NOTICIAS


def media_origin_label(origin: str | None) -> str:
    return {
        YOUTUBE: "YouTube",
        REDE_SOCIAL: "Redes sociais",
        PORTAL_NOTICIAS: "Portal de notícias",
    }.get(origin or "", "Portal de notícias")

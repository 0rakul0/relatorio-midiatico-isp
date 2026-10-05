from __future__ import annotations

from urllib.parse import parse_qs, urlparse


def platform_for_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.lower()
    query = parse_qs(parsed.query)
    if host.endswith("instagram.com") and ("/p/" in path or "/reel/" in path or "/reels/" in path):
        return "instagram"
    if host.endswith("facebook.com") or host.endswith("fb.watch"):
        if ("/posts/" in path or "/reel/" in path or "/reels/" in path or "/videos/" in path or "/share/" in path or "story_fbid" in query or host.endswith("fb.watch")):
            return "facebook"
    if host.endswith("tiktok.com") and "/video/" in path:
        return "tiktok"
    if (host == "x.com" or host.endswith(".x.com") or host.endswith("twitter.com")) and "/status/" in path:
        return "x"
    return None


def url_key(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/")
        return f"{host}{path}".lower()
    except Exception:
        return raw.rstrip("/").lower()

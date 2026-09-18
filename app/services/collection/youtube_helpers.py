from __future__ import annotations

import re
from urllib.parse import urlparse

from app.config import get_settings
from app.media_scout import MediaScout
from app.models import Project
from app.source_registry import PRIORITY_YOUTUBE_CHANNELS, PRIORITY_YOUTUBE_CHANNEL_ALIASES
from app.topic_profile import normalized_text


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
    priority = [task for task in tasks if task.is_priority]
    thematic = [task for task in tasks if not task.is_priority]
    task_budget = max(settings.max_youtube_tasks, len(priority))
    return [*priority, *thematic[: max(0, task_budget - len(priority))]]



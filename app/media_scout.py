from dataclasses import dataclass

from app.source_registry import PRIORITY_MEDIA_SOURCES, PRIORITY_YOUTUBE_CHANNELS


@dataclass(frozen=True)
class ScoutTask:
    platform: str
    query: str
    target: str
    rationale: str
    purpose: str = "MEDIA_REPERCUSSION"


class MediaScout:
    """Planeja buscas de repercussão sem depender da frase exata do tema."""

    def __init__(self, topic: str, profile: dict | None = None):
        self.topic = topic.strip()
        self.profile = profile or {}

    def _query_bases(self) -> list[str]:
        actors = (self.profile.get("actors") or [])[:5]
        actions = (self.profile.get("actions") or [])[:5]
        locations = (self.profile.get("locations") or [])[:3]
        bases = [self.topic]
        if actors and actions:
            for actor in actors[:4]:
                for action in actions[:3]:
                    parts = [actor, action, *locations[:1]]
                    bases.append(" ".join(part for part in parts if part))
        elif self.profile.get("search_synonyms"):
            bases.extend(self.profile["search_synonyms"][:6])
        return list(dict.fromkeys(q.strip() for q in bases if q.strip()))[:12]

    def web_tasks(self) -> list[ScoutTask]:
        bases = self._query_bases()
        tasks: list[ScoutTask] = []
        for base in bases:
            tasks.append(ScoutTask("web", base, "Busca temática", "Localizar cobertura sobre o tema."))
        # Checagem nominal por veículo usa bases compactas para não exigir a frase exata.
        vehicle_base = bases[1] if len(bases) > 1 else bases[0]
        for label, domain in PRIORITY_MEDIA_SOURCES:
            tasks.append(
                ScoutTask(
                    "web",
                    f"site:{domain} {vehicle_base}",
                    label,
                    f"Verificar cobertura no portal {label}.",
                )
            )
        unique: dict[tuple[str, str], ScoutTask] = {}
        for task in tasks:
            unique.setdefault((task.platform, task.query.casefold()), task)
        return list(unique.values())

    def youtube_tasks(self) -> list[ScoutTask]:
        bases = self._query_bases()
        tasks = [ScoutTask("youtube", base, "Busca temática", "Localizar vídeos sobre o tema.") for base in bases[:4]]
        compact = bases[1] if len(bases) > 1 else bases[0]
        tasks.extend(
            ScoutTask("youtube", f"{compact} {channel_query}", label, f"Verificar cobertura no canal {label}.")
            for label, channel_query in PRIORITY_YOUTUBE_CHANNELS
        )
        unique: dict[tuple[str, str], ScoutTask] = {}
        for task in tasks:
            unique.setdefault((task.platform, task.query.casefold()), task)
        return list(unique.values())

    @staticmethod
    def platform_status(youtube_enabled: bool) -> list[dict[str, str]]:
        return [
            {"platform": "Sites jornalísticos", "status": "ativo"},
            {"platform": "YouTube", "status": "ativo" if youtube_enabled else "aguarda YOUTUBE_API_KEY"},
            {"platform": "Instagram", "status": "conector planejado"},
            {"platform": "X", "status": "conector planejado"},
        ]

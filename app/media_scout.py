from __future__ import annotations

import re
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
    """Planeja buscas preservando a identidade do objeto monitorado.

    Em produto institucional, termos de assunto podem ajudar a analise, mas nunca
    viram consultas independentes de repercussao. A busca usa somente variantes
    ancoradas do nome do produto.
    """

    def __init__(self, topic: str, profile: dict | None = None):
        self.profile = profile or {}
        self.topic = " ".join((topic or "").split()).strip()
        self.product_name = " ".join((self.profile.get("product_name") or self.topic).split()).strip()

    @property
    def is_institutional_product(self) -> bool:
        return str(self.profile.get("project_type") or "").upper() == "INSTITUTIONAL_PRODUCT"

    @property
    def is_event_topic(self) -> bool:
        return str(self.profile.get("project_type") or "").upper() == "EVENT_TOPIC"

    def _product_anchor(self) -> str:
        configured = " ".join((self.profile.get("product_anchor") or "").split()).strip()
        if configured:
            return configured
        without_year = re.sub(r"\b20\d{2}\b", "", self.product_name).strip()
        without_year = " ".join(without_year.split())
        return without_year or self.product_name

    @staticmethod
    def _quote(value: str) -> str:
        value = " ".join((value or "").split()).strip()
        return f'"{value}"' if value else ""

    def _institutional_search_bases(self) -> list[str]:
        variants = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("product_search_variants") or [])
            if str(value).strip()
        ]
        if not variants:
            variants = [self.product_name, self._product_anchor()]

        year_match = re.search(r"\b(20\d{2})\b", self.product_name)
        year = year_match.group(1) if year_match else ""
        anchor = self._product_anchor()

        bases: list[str] = []
        for variant in variants:
            if not variant:
                continue
            query = self._quote(variant)
            # Se a variante perdeu o ano, preserva o ano original como contexto.
            if year and year not in variant:
                query = f"{query} {year}"
            bases.append(query.strip())

        if anchor:
            bases.append(f'{self._quote(anchor)} "Instituto de Seguranca Publica"'.strip())

        return list(dict.fromkeys(base for base in bases if base))[:3]

    def _event_search_bases(self) -> list[str]:
        variants = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("event_search_variants") or [])
            if str(value).strip()
        ]
        anchor = " ".join(str(self.profile.get("event_anchor") or "").split()).strip()
        if not variants and anchor:
            variants = [anchor]
        if not variants:
            return [self.topic]

        locations = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("locations") or [])
            if str(value).strip()
        ]
        location = next((value for value in locations if len(value) > 2), "")
        year_match = re.search(r"\b(20\d{2})\b", self.topic)
        year = year_match.group(1) if year_match else ""

        bases: list[str] = []
        for variant in variants[:4]:
            parts = [self._quote(variant)]
            if location and location.casefold() not in variant.casefold():
                parts.append(self._quote(location))
            if year and year not in variant:
                parts.append(year)
            bases.append(" ".join(parts).strip())
        return list(dict.fromkeys(base for base in bases if base))[:4]

    def _query_bases(self) -> list[str]:
        if self.is_institutional_product:
            return self._institutional_search_bases()
        if self.is_event_topic and (self.profile.get("event_anchor") or self.profile.get("event_search_variants")):
            return self._event_search_bases()

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

    def _portal_anchor(self) -> str:
        if self.is_event_topic and (self.profile.get("event_anchor") or self.profile.get("event_search_variants")):
            bases = self._event_search_bases()
            return bases[0]
        if not self.is_institutional_product:
            bases = self._query_bases()
            return bases[1] if len(bases) > 1 else bases[0]

        anchor = self._product_anchor()
        year_match = re.search(r"\b(20\d{2})\b", self.product_name)
        year = year_match.group(1) if year_match else ""
        return f'{self._quote(anchor)} {year}'.strip()

    def web_tasks(self) -> list[ScoutTask]:
        bases = self._query_bases()
        tasks: list[ScoutTask] = []
        for base in bases:
            if self.is_institutional_product:
                rationale = "Localizar cobertura que mencione o produto ou atribua achados a ele/ao ISP."
            elif self.is_event_topic and self.profile.get("event_anchor"):
                rationale = "Localizar cobertura preservando a categoria factual, o território e o período solicitados."
            else:
                rationale = "Localizar cobertura sobre o tema."
            tasks.append(ScoutTask("web", base, "Busca temática", rationale))

        vehicle_base = self._portal_anchor()
        for label, domain in PRIORITY_MEDIA_SOURCES:
            tasks.append(
                ScoutTask(
                    "web",
                    f"site:{domain} {vehicle_base}",
                    label,
                    f"Verificar cobertura no portal {label} mantendo a ancora do objeto monitorado.",
                )
            )

        unique: dict[tuple[str, str], ScoutTask] = {}
        for task in tasks:
            unique.setdefault((task.platform, task.query.casefold()), task)
        return list(unique.values())

    def youtube_tasks(self) -> list[ScoutTask]:
        bases = self._query_bases()
        thematic_limit = 2 if self.is_institutional_product else (3 if self.is_event_topic else 4)
        tasks = [
            ScoutTask(
                "youtube",
                base,
                "Busca temática",
                "Localizar videos materialmente relacionados ao objeto monitorado.",
            )
            for base in bases[:thematic_limit]
        ]
        compact = self._portal_anchor()
        tasks.extend(
            ScoutTask(
                "youtube",
                f"{compact} {channel_query}",
                label,
                f"Verificar cobertura no canal {label} mantendo a ancora do objeto monitorado.",
            )
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
            {"platform": "YouTube", "status": "ativo" if youtube_enabled else "indisponível"},
            {"platform": "Instagram", "status": "conector planejado"},
            {"platform": "X", "status": "conector planejado"},
        ]

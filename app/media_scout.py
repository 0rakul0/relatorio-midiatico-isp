from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.source_registry import (
    ISP_INSTITUTION_NAME,
    PRIORITY_MEDIA_SOURCES,
)
from app.topic_profile import normalized_text, product_anchor_from_name
from app.year_utils import find_year


@dataclass(frozen=True)
class ScoutTask:
    platform: str
    query: str
    target: str
    rationale: str
    purpose: str = "MEDIA_REPERCUSSION"
    is_priority: bool = False
    role: str = "THEMATIC"


class MediaScout:
    """Builds a compact, auditable search plan.

    The LLM (when enabled) chooses the search strategy: one primary query and a
    very small number of materially different complementary queries. MediaScout
    then expands only deterministic portal coverage checks. YouTube and social
    sources are routed from the same DuckDuckGo discovery results.
    """

    def __init__(self, topic: str, profile: dict | None = None):
        self.profile = profile or {}
        self.topic = " ".join((topic or "").split()).strip()
        self.product_name = " ".join(
            (self.profile.get("product_name") or self.topic).split()
        ).strip()

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
        return product_anchor_from_name(self.product_name) or self.product_name

    @staticmethod
    def _quote(value: str) -> str:
        value = " ".join((value or "").split()).strip()
        return f'"{value}"' if value else ""

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            compact = " ".join(str(value or "").split()).strip()
            key = normalized_text(compact)
            if not compact or not key or key in seen:
                continue
            seen.add(key)
            output.append(compact)
        return output

    def _with_event_context(self, variant: str) -> str:
        variant = " ".join(str(variant or "").split()).strip()
        if not variant:
            return ""
        locations = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("locations") or [])
            if str(value).strip()
        ]
        location = next((value for value in locations if len(value) > 2), "")
        year = find_year(self.topic) or ""
        parts = [self._quote(variant)]
        if location and normalized_text(location) not in normalized_text(variant):
            parts.append(self._quote(location))
        if year and year not in variant:
            parts.append(year)
        return " ".join(part for part in parts if part).strip()

    def _institutional_fallback_queries(self) -> list[str]:
        variants = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("product_search_variants") or [])
            if str(value).strip()
        ]
        anchor = self._product_anchor()
        if not variants:
            variants = [self.product_name, anchor]
        year = find_year(self.product_name) or ""
        queries: list[str] = []
        for variant in variants:
            query = self._quote(variant)
            if year and year not in variant:
                query = f"{query} {year}"
            queries.append(query.strip())
        if anchor:
            queries.append(f'{self._quote(anchor)} "{ISP_INSTITUTION_NAME}"'.strip())
        return self._dedupe(queries)

    def _event_fallback_queries(self) -> list[str]:
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
        return self._dedupe([self._with_event_context(value) for value in variants])

    def _general_fallback_queries(self) -> list[str]:
        """Amplia tema geral sem soltar palavras isoladas.

        O objetivo é aumentar recall para pautas amplas mantendo cada consulta
        semanticamente ancorada. Primeiro preservamos as variantes do perfil;
        depois combinamos atores, ações e organizações com o contexto temporal
        e territorial do tema.
        """
        values = [
            str(value).strip()
            for value in (self.profile.get("search_synonyms") or [])
            if str(value).strip()
        ]
        values.append(self.topic)

        locations = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("locations") or [])
            if str(value).strip()
        ]
        location = next((value for value in locations if len(value) > 2), "")
        year = find_year(self.topic) or ""

        def contextual_query(*parts: str) -> str:
            query_parts = [self._quote(part) for part in parts if str(part).strip()]
            if location and all(
                normalized_text(location) not in normalized_text(part)
                for part in parts
            ):
                query_parts.append(self._quote(location))
            if year and all(year not in str(part) for part in parts):
                query_parts.append(year)
            return " ".join(part for part in query_parts if part).strip()

        actors = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("actors") or [])
            if str(value).strip()
        ][:4]
        actions = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("actions") or [])
            if str(value).strip()
        ][:4]
        organizations = [
            " ".join(str(value).split()).strip()
            for value in (self.profile.get("organizations") or [])
            if str(value).strip()
        ][:3]

        for action in actions:
            for actor in actors:
                values.append(contextual_query(action, actor))

        anchor = next(
            (
                value
                for value in (self.profile.get("search_synonyms") or [])
                if str(value).strip()
            ),
            self.topic,
        )
        for organization in organizations:
            values.append(contextual_query(str(anchor), organization))

        return self._dedupe(values)

    def fallback_search_strategy(self, max_complementary: int = 2) -> dict[str, Any]:
        if self.is_institutional_product:
            bases = self._institutional_fallback_queries()
        elif self.is_event_topic:
            bases = self._event_fallback_queries()
        else:
            bases = self._general_fallback_queries()

        primary = bases[0] if bases else self.topic
        complementary = bases[1 : 1 + max(0, max_complementary)]

        fact_query: str | None = None
        official_query: str | None = None
        if self.is_event_topic:
            fact_variants = [
                " ".join(str(value).split()).strip()
                for value in (self.profile.get("fact_discovery_variants") or [])
                if str(value).strip()
            ]
            fact_query = (
                self._with_event_context(fact_variants[0])
                if fact_variants
                else primary
            )
            official_query = primary

        return {
            "primary_query": primary,
            "complementary_queries": complementary,
            "fact_query": fact_query,
            "official_query": official_query,
            "rationale": "Deterministic fallback strategy preserving the monitored object anchor.",
        }

    def search_strategy(self, max_complementary: int = 2) -> dict[str, Any]:
        fallback = self.fallback_search_strategy(max_complementary=max_complementary)
        stored = self.profile.get("search_strategy")
        if not isinstance(stored, dict):
            return fallback

        primary = " ".join(str(stored.get("primary_query") or "").split()).strip()
        complementary = self._dedupe(
            [str(value) for value in (stored.get("complementary_queries") or [])]
        )[: max(0, max_complementary)]
        return {
            "primary_query": primary or fallback["primary_query"],
            "complementary_queries": complementary,
            "fact_query": stored.get("fact_query") or fallback.get("fact_query"),
            "official_query": stored.get("official_query") or fallback.get("official_query"),
            "rationale": str(stored.get("rationale") or fallback["rationale"]),
        }

    def _query_bases(self, max_complementary: int = 2) -> list[str]:
        strategy = self.search_strategy(max_complementary=max_complementary)
        return self._dedupe(
            [strategy["primary_query"], *strategy.get("complementary_queries", [])]
        )

    def _portal_anchor(self) -> str:
        strategy = self.search_strategy(max_complementary=0)
        return str(strategy["primary_query"]).strip() or self.topic

    def web_tasks(self, max_complementary: int = 2) -> list[ScoutTask]:
        bases = self._query_bases(max_complementary=max_complementary)
        if not bases:
            bases = [self.topic]

        tasks: list[ScoutTask] = []
        primary = bases[0]
        tasks.append(
            ScoutTask(
                "web",
                primary,
                "Busca principal",
                "Consulta principal otimizada para localizar varias materias relevantes.",
                role="PRIMARY",
            )
        )

        for query in bases[1 : 1 + max(0, max_complementary)]:
            tasks.append(
                ScoutTask(
                    "web",
                    query,
                    "Busca complementar",
                    "Cobrir uma formulacao materialmente diferente sem repetir parafrases equivalentes.",
                    role="COMPLEMENTARY",
                )
            )

        # Portal coverage is deterministic. The LLM does not need to invent one
        # query per outlet; it only optimizes the primary query above.
        for label, domain in PRIORITY_MEDIA_SOURCES:
            tasks.append(
                ScoutTask(
                    "web",
                    f"site:{domain} {primary}",
                    label,
                    f"Verificar cobertura no portal {label} usando a consulta principal.",
                    is_priority=True,
                    role="PRIORITY_PORTAL",
                )
            )

        unique: dict[tuple[str, str], ScoutTask] = {}
        for task in tasks:
            unique.setdefault((task.platform, normalized_text(task.query)), task)
        return list(unique.values())

    @staticmethod
    def platform_status(youtube_enabled: bool) -> list[dict[str, str]]:
        return [
            {"platform": "Sites jornalisticos", "status": "descoberta via DuckDuckGo"},
            {"platform": "YouTube", "status": "roteado da descoberta DuckDuckGo"},
            {"platform": "Instagram", "status": "DuckDuckGo -> Apify"},
            {"platform": "Facebook", "status": "DuckDuckGo -> Apify"},
            {"platform": "TikTok", "status": "DuckDuckGo -> Apify"},
            {"platform": "X", "status": "DuckDuckGo -> Apify"},
        ]

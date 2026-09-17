from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date

from app.llm import llm_is_configured, structured_response
from app.prompts import TOPIC_PROFILE_PROMPT


MONTHS_PT = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

GENERIC_PRODUCT_TERMS = {
    "dossie",
    "relatorio",
    "boletim",
    "anuario",
    "estudo",
    "publicacao",
    "pesquisa",
    "documento",
    "edicao",
    "serie",
    "instituto",
    "seguranca",
    "publica",
    "rio",
    "janeiro",
}


def normalized_text(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", (text or "").lower())
        if not unicodedata.combining(char)
    )


def normalized_terms(text: str) -> set[str]:
    return {
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]+", normalized_text(text))
        if len(token) >= 3 and not token.isdigit()
    }


def requested_month_window(topic: str) -> tuple[date, date] | None:
    normalized = normalized_text(topic)
    pattern = re.compile(r"\b(" + "|".join(MONTHS_PT) + r")\s+(?:de\s+)?(20\d{2})\b")
    matches: list[tuple[int, int]] = []
    for match in pattern.finditer(normalized):
        month_name, year_text = match.groups()
        # Desambiguação necessária para consultas como
        # "... no Rio de Janeiro 2026": nesse caso "Janeiro 2026" é parte
        # do topônimo e não uma referência ao mês de janeiro.
        prefix = normalized[max(0, match.start() - 24): match.start()]
        if month_name == "janeiro" and re.search(r"\brio\s+de\s+$", prefix):
            continue
        matches.append((MONTHS_PT[month_name], int(year_text)))

    unique = set(matches)
    if len(unique) != 1:
        return None
    month, year = unique.pop()
    last_day = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def requested_year_window(topic: str) -> tuple[date, date] | None:
    """Extrai um único ano explícito como janela anual completa.

    A função é propositalmente simples; o chamador decide se o tipo de pauta
    pode usar o ano como recorte. Assim, "Dossiê Mulher 2026" não vira
    automaticamente uma janela anual só porque contém um ano.
    """
    normalized = normalized_text(topic)
    years = {int(year) for year in re.findall(r"(?<!\d)(20\d{2})(?!\d)", normalized)}
    if len(years) != 1:
        return None
    year = years.pop()
    return date(year, 1, 1), date(year, 12, 31)


def _heuristic_project_type(topic: str) -> str:
    text = normalized_text(topic)
    product_terms = ("dossie", "relatorio", "boletim", "anuario", "estudo", "publicacao")
    event_terms = (
        "morto", "morta", "mortos", "mortas", "morte", "assassinado", "assassinada",
        "operacao", "apreensao", "prisao", "vitima", "ferido", "feminicidio", "homicidio",
        "intervencao de agente do estado", "intervencao policial", "letalidade policial",
    )
    if any(term in text for term in product_terms):
        return "INSTITUTIONAL_PRODUCT"
    if requested_month_window(topic) or any(term in text for term in event_terms):
        return "EVENT_TOPIC"
    return "GENERAL_TOPIC"


def requested_topic_window(topic: str) -> tuple[date, date] | None:
    """Resolve mês explícito primeiro e ano completo apenas para pauta factual.

    Exemplos:
    - "mortes ... agosto de 2026" -> 2026-08-01..2026-08-31
    - "morte por intervenção ... 2026" -> 2026-01-01..2026-12-31
    - "Dossiê Mulher 2026" -> None (produto institucional, busca temática)
    """
    month_window = requested_month_window(topic)
    if month_window:
        return month_window
    if _heuristic_project_type(topic) != "EVENT_TOPIC":
        return None
    return requested_year_window(topic)


def _clean_phrase(value: str | None) -> str:
    return " ".join((value or "").split()).strip()


def product_anchor_from_name(product_name: str | None) -> str | None:
    """Deriva uma ancora nominal estavel sem transformar o produto em tokens soltos.

    Ex.: ``Dossie Mulher 2026`` -> ``Dossie Mulher``.
    """
    name = _clean_phrase(product_name)
    if not name:
        return None
    no_year = re.sub(r"\b20\d{2}\b", "", name)
    no_year = _clean_phrase(no_year)
    return no_year or name


def _anchored_product_variants(product_name: str, product_anchor: str | None) -> list[str]:
    variants = [product_name]
    if product_anchor and normalized_text(product_anchor) != normalized_text(product_name):
        variants.append(product_anchor)
    return list(dict.fromkeys(_clean_phrase(value) for value in variants if _clean_phrase(value)))


def _subject_terms_from_product(product_name: str) -> list[str]:
    terms = sorted(normalized_terms(product_name))
    return [term for term in terms if term not in GENERIC_PRODUCT_TERMS]


STATE_INTERVENTION_EVENT_VARIANTS = [
    "morte por intervenção de agente do Estado",
    "mortes por intervenção de agentes do Estado",
    "morte decorrente de intervenção policial",
    "mortes decorrentes de intervenção policial",
]

STATE_INTERVENTION_FACT_VARIANTS = [
    *STATE_INTERVENTION_EVENT_VARIANTS,
    "morto em intervenção policial",
    "morto durante intervenção policial",
    "morreu após intervenção policial",
    "morto durante ação policial",
    "morte em ação policial",
]


def _is_state_intervention_death_topic(topic: str) -> bool:
    text = " ".join(normalized_text(topic).split())
    death_hit = any(term in text for term in ("morte", "mortes", "morto", "morta", "letalidade"))
    intervention_hit = any(
        term in text
        for term in (
            "intervencao de agente do estado",
            "intervencao de agentes do estado",
            "intervencao policial",
            "acao policial",
            "letalidade policial",
        )
    )
    return death_hit and intervention_hit


def _event_profile_from_topic(topic: str) -> dict:
    """Cria âncoras determinísticas para eventos conhecidos.

    O objetivo é impedir que o planejador reduza uma categoria factual específica
    a buscas genéricas como "morte Rio" ou "polícia 2026".
    """
    if _is_state_intervention_death_topic(topic):
        return {
            "event_type": "DEATH_BY_STATE_INTERVENTION",
            "event_anchor": "morte por intervenção de agente do Estado",
            "event_search_variants": list(STATE_INTERVENTION_EVENT_VARIANTS),
            "fact_discovery_variants": list(STATE_INTERVENTION_FACT_VARIANTS),
            "actors": [
                "agente do Estado",
                "policial",
                "policial militar",
                "policial civil",
            ],
            "actions": [
                "morte por intervenção de agente do Estado",
                "morte decorrente de intervenção policial",
                "morto durante intervenção policial",
                "morreu após intervenção policial",
            ],
            "organizations": ["ISP", "PMERJ", "Polícia Civil RJ"],
        }
    return {
        "event_type": "OTHER",
        "event_anchor": None,
        "event_search_variants": [],
        "fact_discovery_variants": [],
        "actors": [],
        "actions": [],
        "organizations": [],
    }


def heuristic_topic_profile(topic: str) -> dict:
    text = normalized_text(topic)
    project_type = _heuristic_project_type(topic)
    product_name = _clean_phrase(topic) if project_type == "INSTITUTIONAL_PRODUCT" else None
    product_anchor = product_anchor_from_name(product_name)
    product_search_variants = (
        _anchored_product_variants(product_name, product_anchor)
        if product_name
        else []
    )
    subject_terms = _subject_terms_from_product(product_name) if product_name else []

    event_profile = _event_profile_from_topic(topic) if project_type == "EVENT_TOPIC" else _event_profile_from_topic("")
    actors: list[str] = list(event_profile["actors"])
    actions: list[str] = list(event_profile["actions"])
    organizations: list[str] = list(event_profile["organizations"])
    requested_fact_fields: list[str] = []
    search_synonyms: list[str] = []
    event_type = event_profile["event_type"]
    event_anchor = event_profile["event_anchor"]
    event_search_variants = list(event_profile["event_search_variants"])
    fact_discovery_variants = list(event_profile["fact_discovery_variants"])

    if not actors and ("policial" in text or "policia" in text):
        actors = ["policial", "policial militar", "PM", "policial civil", "inspetor", "policial penal", "agente"]
        organizations = ["PMERJ", "Policia Civil RJ", "SEAP RJ"]
    if event_type == "OTHER" and any(term in text for term in ("morto", "morta", "mortos", "mortas", "morte", "assassinado", "assassinada")):
        event_type = "DEATH"
        actions = ["morto", "morreu", "assassinado", "falecido", "baleado", "morte"]

    if event_type in {"DEATH", "DEATH_BY_STATE_INTERVENTION"}:
        requested_fact_fields = [
            "subject_name", "institution", "rank_or_role", "unit", "professional_status",
            "event_date", "death_date", "cause_description", "circumstance",
            "address", "neighborhood", "city", "state",
            "death_place_name", "death_address", "death_neighborhood", "death_city", "death_state",
        ]

    locations = []
    if "rio de janeiro" in text or re.search(r"\brj\b", text):
        locations = ["Rio de Janeiro", "RJ", "estado do Rio de Janeiro"]

    if project_type == "INSTITUTIONAL_PRODUCT":
        # Compatibilidade com codigo antigo: em produto institucional,
        # search_synonyms contem somente variantes ancoradas do produto.
        search_synonyms = list(product_search_variants)
    elif project_type == "EVENT_TOPIC" and event_search_variants:
        # Para evento conhecido, sinônimos de busca continuam semanticamente
        # presos à categoria factual; atores/ações isolados não viram consultas.
        search_synonyms = list(event_search_variants)
    elif not search_synonyms:
        search_synonyms = sorted(normalized_terms(topic))

    return {
        "project_type": project_type,
        "product_name": product_name,
        "product_anchor": product_anchor,
        "product_search_variants": product_search_variants,
        "subject_terms": subject_terms,
        "event_type": event_type,
        "event_anchor": event_anchor,
        "event_search_variants": event_search_variants,
        "fact_discovery_variants": fact_discovery_variants,
        "actors": actors,
        "actions": actions,
        "locations": locations,
        "organizations": organizations,
        "search_synonyms": list(dict.fromkeys(search_synonyms))[:30],
        "requested_fact_fields": requested_fact_fields,
        "inclusion_rules": {"professional_status": [], "notes": []},
        "exclusion_rules": {"professional_status": [], "notes": []},
    }


def build_topic_profile(topic: str) -> dict:
    fallback = heuristic_topic_profile(topic)
    if not llm_is_configured():
        return fallback

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_type": {
                "type": "string",
                "enum": ["INSTITUTIONAL_PRODUCT", "EVENT_TOPIC", "GENERAL_TOPIC"],
            },
            "product_name": {"type": ["string", "null"]},
            "product_anchor": {"type": ["string", "null"]},
            "product_search_variants": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "subject_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "event_type": {"type": "string"},
            "event_anchor": {"type": ["string", "null"]},
            "event_search_variants": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "fact_discovery_variants": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "actors": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "actions": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "locations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "organizations": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "search_synonyms": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "requested_fact_fields": {"type": "array", "items": {"type": "string"}, "maxItems": 30},
            "inclusion_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "professional_status": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                },
                "required": ["professional_status", "notes"],
            },
            "exclusion_rules": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "professional_status": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                    "notes": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                },
                "required": ["professional_status", "notes"],
            },
        },
        "required": [
            "project_type", "product_name", "product_anchor", "product_search_variants", "subject_terms",
            "event_type", "event_anchor", "event_search_variants", "fact_discovery_variants",
            "actors", "actions", "locations", "organizations", "search_synonyms",
            "requested_fact_fields", "inclusion_rules", "exclusion_rules",
        ],
    }
    try:
        result = structured_response(
            instructions=TOPIC_PROFILE_PROMPT,
            payload={"topic": topic},
            schema_name="topic_profile_v2",
            schema=schema,
        )
    except RuntimeError:
        return fallback

    # Evita que um perfil LLM pobre elimine pistas uteis do fallback.
    for key in ("actors", "actions", "locations", "organizations", "requested_fact_fields"):
        result[key] = list(dict.fromkeys([*(result.get(key) or []), *(fallback.get(key) or [])]))

    if result.get("project_type") == "GENERAL_TOPIC" and fallback["project_type"] == "EVENT_TOPIC":
        result["project_type"] = "EVENT_TOPIC"

    if result.get("project_type") == "INSTITUTIONAL_PRODUCT":
        # Hard guard: o nome pode vir do LLM, mas a ancora e as variantes de busca
        # sao normalizadas deterministicamente. Termos de assunto ficam separados.
        product_name = _clean_phrase(result.get("product_name")) or _clean_phrase(topic)
        # O LLM pode resumir o nome do produto, mas nao pode trocar o objeto
        # pedido por outro. Se introduzir termos nominais que nao aparecem no
        # tema original, voltamos ao texto do usuario.
        topic_terms = normalized_terms(topic)
        candidate_terms = normalized_terms(product_name)
        if candidate_terms and not candidate_terms.issubset(topic_terms):
            product_name = _clean_phrase(topic)
        product_anchor = product_anchor_from_name(product_name)
        deterministic_variants = _anchored_product_variants(product_name, product_anchor)

        llm_variants = []
        anchor_norm = normalized_text(product_anchor or "")
        for candidate in result.get("product_search_variants") or []:
            candidate_clean = _clean_phrase(candidate)
            if not candidate_clean:
                continue
            candidate_norm = normalized_text(candidate_clean)
            if anchor_norm and anchor_norm in candidate_norm:
                llm_variants.append(candidate_clean)

        subject_terms = list(dict.fromkeys([
            *(result.get("subject_terms") or []),
            *fallback.get("subject_terms", []),
        ]))

        variants = list(dict.fromkeys([*deterministic_variants, *llm_variants]))[:10]
        result["product_name"] = product_name
        result["product_anchor"] = product_anchor
        result["product_search_variants"] = variants
        result["subject_terms"] = subject_terms[:20]
        # Compatibilidade: nunca deixa search_synonyms virar tokens soltos.
        result["search_synonyms"] = list(variants)
    else:
        result["product_name"] = None
        result["product_anchor"] = None
        result["product_search_variants"] = []
        result["subject_terms"] = list(dict.fromkeys([
            *(result.get("subject_terms") or []),
            *fallback.get("subject_terms", []),
        ]))[:20]

        if result.get("project_type") == "EVENT_TOPIC":
            # Quando a heurística reconhece uma categoria factual específica,
            # ela prevalece sobre simplificações do LLM. Isso preserva MIAE como
            # categoria e impede que a busca seja reduzida a "morte"/"polícia".
            deterministic_anchor = fallback.get("event_anchor")
            if deterministic_anchor:
                result["event_type"] = fallback.get("event_type") or result.get("event_type")
                result["event_anchor"] = deterministic_anchor
                result["event_search_variants"] = list(dict.fromkeys(
                    fallback.get("event_search_variants") or []
                ))[:12]
                result["fact_discovery_variants"] = list(dict.fromkeys(
                    fallback.get("fact_discovery_variants") or []
                ))[:16]
                result["search_synonyms"] = list(result["event_search_variants"])
            else:
                result["event_anchor"] = _clean_phrase(result.get("event_anchor")) or None
                result["event_search_variants"] = list(dict.fromkeys([
                    *(result.get("event_search_variants") or []),
                    *(fallback.get("event_search_variants") or []),
                ]))[:12]
                result["fact_discovery_variants"] = list(dict.fromkeys([
                    *(result.get("fact_discovery_variants") or []),
                    *(fallback.get("fact_discovery_variants") or []),
                ]))[:16]
                result["search_synonyms"] = list(dict.fromkeys([
                    *(result.get("search_synonyms") or []),
                    *(fallback.get("search_synonyms") or []),
                ]))[:30]
        else:
            result["event_anchor"] = None
            result["event_search_variants"] = []
            result["fact_discovery_variants"] = []
            result["search_synonyms"] = list(dict.fromkeys([
                *(result.get("search_synonyms") or []),
                *(fallback.get("search_synonyms") or []),
            ]))[:30]

    return result

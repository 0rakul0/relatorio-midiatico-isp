from __future__ import annotations

from datetime import date

from app.agent import get_report_agent
from app.llm import llm_is_configured
from app.schemas import TopicProfileResponse
from app.year_utils import strip_year
from app.topics.temporal import (
    normalized_terms,
    normalized_text,
    requested_month_window,
    requested_year_window,
)
from app.topics.locations import (
    canonicalize_known_locations,
    clean_phrase as _clean_phrase,
    location_topic_variants,
)


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


def _heuristic_project_type(topic: str) -> str:
    text = normalized_text(topic)
    product_terms = ("dossie", "relatorio", "boletim", "anuario", "estudo", "publicacao")
    event_terms = (
        "morto", "morta", "mortos", "mortas", "morte", "assassinado", "assassinada",
        "operacao", "apreensao", "prisao", "vitima", "ferido", "feminicidio", "homicidio",
        "intervencao de agente do estado", "intervencao policial", "letalidade policial",
    )
    # Categorias factuais conhecidas e eventos explícitos prevalecem sobre termos
    # genéricos de produto. Um título como "relatório sobre mortes ..." é um fato
    # noticiado, não um produto institucional; caso contrário, a palavra
    # "relatório" sozinha forçaria INSTITUTIONAL_PRODUCT.
    if _is_state_intervention_death_topic(topic):
        return "EVENT_TOPIC"
    if any(term in text for term in event_terms):
        return "EVENT_TOPIC"
    if requested_month_window(topic):
        return "EVENT_TOPIC"
    if any(term in text for term in product_terms):
        return "INSTITUTIONAL_PRODUCT"
    return "GENERAL_TOPIC"


def requested_topic_window(topic: str) -> tuple[date, date] | None:
    """Resolve o recorte temporal explícito sem inventar datas para produtos.

    Regras:
    - mês explícito prevalece;
    - EVENT_TOPIC com um ano explícito usa o ano completo;
    - GENERAL_TOPIC com um ano explícito também usa esse ano, pois o usuário
      pediu um panorama temporal definido;
    - no ano corrente, a janela termina hoje para impedir que datas futuras
      entrem no relatório;
    - INSTITUTIONAL_PRODUCT continua temático: o ano pode ser parte do nome da
      edição e não deve virar automaticamente uma janela editorial.
    """
    month_window = requested_month_window(topic)
    if month_window:
        return month_window

    project_type = _heuristic_project_type(topic)
    if project_type == "INSTITUTIONAL_PRODUCT":
        return None

    year_window = requested_year_window(topic)
    if not year_window:
        return None

    start, end = year_window
    today = date.today()
    if start.year == today.year:
        end = min(end, today)
    return start, end


def product_anchor_from_name(product_name: str | None) -> str | None:
    """Deriva uma ancora nominal estavel sem transformar o produto em tokens soltos.

    Ex.: ``Dossie Mulher 2026`` -> ``Dossie Mulher``.
    """
    name = _clean_phrase(product_name)
    if not name:
        return None
    no_year = strip_year(name)
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

    locations, topic_search_variants = location_topic_variants(topic)

    if project_type == "INSTITUTIONAL_PRODUCT":
        # Compatibilidade com codigo antigo: em produto institucional,
        # search_synonyms contem somente variantes ancoradas do produto.
        search_synonyms = list(product_search_variants)
    elif project_type == "EVENT_TOPIC" and event_search_variants:
        # Para evento conhecido, sinônimos de busca continuam semanticamente
        # presos à categoria factual; atores/ações isolados não viram consultas.
        search_synonyms = list(event_search_variants)
    elif project_type == "EVENT_TOPIC":
        # Evento sem familia deterministica conhecida: preserve a consulta inteira
        # e suas correcoes geograficas, nunca transforme o tema em tokens soltos.
        search_synonyms = list(topic_search_variants)
    else:
        # Tema geral deve permanecer ancorado. Consultas de uma palavra isolada
        # (ex.: "habitacional", "milicia") produzem muito ruido.
        search_synonyms = list(topic_search_variants)

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

    try:
        result = get_report_agent().run(
            task="topic_profile",
            payload={"topic": topic},
            schema_name="topic_profile_v2",
            response_model=TopicProfileResponse,
        )
    except RuntimeError:
        return fallback

    # Evita que um perfil LLM pobre elimine pistas uteis do fallback.
    for key in ("actors", "actions", "locations", "organizations", "requested_fact_fields"):
        if key == "locations":
            # O fallback deterministico vem primeiro para que uma grafia
            # canonica conhecida seja a ancora territorial preferencial.
            values = [*(fallback.get(key) or []), *(result.get(key) or [])]
        else:
            values = [*(result.get(key) or []), *(fallback.get(key) or [])]
        result[key] = list(dict.fromkeys(values))

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
            # Em tema geral, priorize variantes ancoradas e corrigidas do
            # fallback. Sugestoes da LLM entram depois e nunca substituem a
            # consulta canonica conhecida.
            result["search_synonyms"] = list(dict.fromkeys([
                *(fallback.get("search_synonyms") or []),
                *(result.get("search_synonyms") or []),
            ]))[:30]

    return result

from __future__ import annotations

import re

from app.models import Project
from app.source_registry import ISP_INSTITUTION_NAME
from app.topic_profile import (
    GENERIC_PRODUCT_TERMS,
    normalized_terms,
    normalized_text,
    product_anchor_from_name,
)
from app.year_utils import find_year, find_years


def _institutional_product_profile(project: Project) -> tuple[str, str, list[str], list[str]]:
    """Retorna nome, ancora, variantes de busca e termos de assunto do produto.

    A separacao e proposital: variantes do produto podem orientar a descoberta;
    termos de assunto servem apenas para confirmar atribuicao/relevancia e nunca
    devem virar buscas autonomas de repercussao.
    """
    profile = project.topic_profile or {}
    product_name = " ".join(str(profile.get("product_name") or project.topic or "").split()).strip()
    product_anchor = " ".join(str(profile.get("product_anchor") or "").split()).strip()
    if not product_anchor:
        product_anchor = product_anchor_from_name(product_name) or product_name

    variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("product_search_variants") or [])
        if str(value).strip()
    ]
    if not variants:
        variants = [product_name, product_anchor]
    variants = list(dict.fromkeys(value for value in variants if value))

    subject_terms = [
        normalized_text(str(value)).strip()
        for value in (profile.get("subject_terms") or [])
        if str(value).strip()
    ]
    if not subject_terms:
        subject_terms = sorted(
            term for term in normalized_terms(product_name) if term not in GENERIC_PRODUCT_TERMS
        )
    return product_name, product_anchor, variants, subject_terms


def _query_preserves_institutional_anchor(project: Project, query_text: str) -> bool:
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True

    _, anchor, variants, _ = _institutional_product_profile(project)
    # Remove apenas o operador site:, preservando o restante da consulta.
    query_norm = normalized_text(re.sub(r"(?:^|\s)site:[^\s]+", " ", query_text or ""))
    query_norm = " ".join(query_norm.split())
    acceptable = [anchor, *variants]
    for candidate in acceptable:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in query_norm:
            return True
    return False


def _event_topic_profile(project: Project) -> tuple[str, list[str], list[str], list[str], str]:
    profile = project.topic_profile or {}
    anchor = " ".join(str(profile.get("event_anchor") or "").split()).strip()
    variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("event_search_variants") or [])
        if str(value).strip()
    ]
    fact_variants = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("fact_discovery_variants") or [])
        if str(value).strip()
    ]
    locations = [
        " ".join(str(value).split()).strip()
        for value in (profile.get("locations") or [])
        if str(value).strip()
    ]
    year = ""
    if project.event_start and project.event_end and project.event_start.year == project.event_end.year:
        year = str(project.event_start.year)
    else:
        year = find_year(project.topic or "") or ""
    return anchor, variants, fact_variants, locations, year


def _query_preserves_event_anchor(project: Project, query_text: str, *, purpose: str) -> bool:
    if project.project_type != "EVENT_TOPIC":
        return True

    anchor, variants, fact_variants, locations, year = _event_topic_profile(project)
    if not anchor and not variants:
        return True

    query_norm = normalized_text(re.sub(r"(?:^|\s)site:[^\s]+", " ", query_text or ""))
    query_norm = " ".join(query_norm.split())
    acceptable = [anchor, *variants]
    if purpose == "FACT_DISCOVERY":
        acceptable.extend(fact_variants)

    semantic_hit = any(
        (candidate_norm := " ".join(normalized_text(candidate).split()))
        and candidate_norm in query_norm
        for candidate in acceptable
    )
    if not semantic_hit:
        return False

    # Se o pedido trouxe território, ele precisa continuar presente na consulta.
    if locations:
        location_hit = False
        query_tokens = set(re.findall(r"[a-z0-9]+", query_norm))
        for location in locations:
            location_norm = " ".join(normalized_text(location).split())
            if location_norm and location_norm in query_norm:
                location_hit = True
                break
            if location_norm == "rj" and "rj" in query_tokens:
                location_hit = True
                break
        if not location_hit:
            return False

    # Ano explícito/inferido também é preservado. O filtro de data do provedor
    # continua existindo, mas a consulta textual fica auditável e específica.
    if year and year not in query_norm:
        return False
    return True


def query_preserves_project_anchor(project: Project, query_text: str, *, purpose: str) -> bool:
    if purpose not in {"MEDIA_REPERCUSSION", "FACT_DISCOVERY", "OFFICIAL_FACT"}:
        return True
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        return purpose != "MEDIA_REPERCUSSION" or _query_preserves_institutional_anchor(project, query_text)
    if project.project_type == "EVENT_TOPIC":
        return _query_preserves_event_anchor(project, query_text, purpose=purpose)
    return True


def institutional_product_version_guard_text(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> tuple[bool, str | None]:
    """Bloqueia edicoes divergentes do mesmo produto institucional.

    Exemplo: se o objeto for ``Dossie Mulher 2026``, um item que trate
    explicitamente apenas de ``Dossie Mulher 2025`` e rejeitado antes de ser
    persistido ou enviado ao LLM. Uma comparacao 2025 x 2026 continua
    permitida quando a edicao-alvo tambem aparece materialmente no texto.
    """
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True, None

    product_name, product_anchor, _, _ = _institutional_product_profile(project)
    requested_years = find_years(product_name or "")
    if not requested_years:
        requested_years = find_years(project.topic or "")
    if not requested_years:
        return True, None

    requested_year = requested_years[0]
    product_base = " ".join(normalized_text(product_anchor or product_name).split()).strip()
    if len(product_base) < 5:
        return True, None

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return True, None

    # Captura somente anos associados nominalmente ao mesmo produto. Assim,
    # um simples ano de publicacao no restante da pagina nao vira evidencia
    # de que a edicao correta foi mencionada.
    pattern = rf"{re.escape(product_base)}\s+(20\d{{2}})"
    mentioned_editions = set(re.findall(pattern, haystack))

    if mentioned_editions and requested_year not in mentioned_editions:
        editions = ", ".join(sorted(mentioned_editions))
        return (
            False,
            f"Edicao incompatível com o produto solicitado: edicao-alvo {requested_year}; "
            f"edicao(oes) encontrada(s): {editions}.",
        )

    return True, None


def _institutional_collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    """Filtro deterministico barato antes de persistir ou enviar o item ao LLM.

    Aceita imediatamente quando o nome/ancora do produto aparece. Quando isso
    nao ocorre, permite apenas atribuicao plausivel: ISP/instituicao + termo de
    assunto do produto + linguagem de atribuicao/estudo. Assim, um resultado
    como "Dossie Master" e rejeitado antes de consumir validacao semantica,
    enquanto uma materia cujo titulo nao cite o produto ainda pode seguir se o
    corpo disser, por exemplo, "segundo levantamento do ISP...".
    """
    if project.project_type != "INSTITUTIONAL_PRODUCT":
        return True

    version_ok, _ = institutional_product_version_guard_text(
        project,
        title=title,
        snippet=snippet,
        content=content,
    )
    if not version_ok:
        return False

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return False

    product_name, anchor, variants, subject_terms = _institutional_product_profile(project)
    nominal_candidates = [product_name, anchor, *variants]
    for candidate in nominal_candidates:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in haystack:
            return True

    institution_terms = {
        normalized_text(project.institution or ""),
        normalized_text(ISP_INSTITUTION_NAME),
        "isp rj",
    }
    profile = project.topic_profile or {}
    institution_terms.update(
        normalized_text(str(value))
        for value in (profile.get("organizations") or [])
        if str(value).strip()
    )
    institution_hit = any(
        term and len(term) >= 5 and term in haystack
        for term in institution_terms
    ) or bool(re.search(r"\bisp(?:-rj)?\b", haystack))

    subject_hit = any(
        term and len(term) >= 4 and term in haystack
        for term in subject_terms
    )

    body_terms = normalized_terms(body)
    attribution_words = {
        "dossie", "relatorio", "boletim", "estudo", "levantamento", "pesquisa",
        "dados", "aponta", "apontou", "segundo", "conforme", "divulgado", "divulgou",
    }
    attribution_hit = bool(body_terms.intersection(attribution_words))

    return bool(institution_hit and subject_hit and attribution_hit)


def _event_collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    """Pré-filtro barato para EVENT_TOPIC com âncora factual conhecida.

    Não exige a redação oficial literal: aceita também formulações jornalísticas
    equivalentes, mas bloqueia resultados que só compartilham palavras amplas
    como "morte", "polícia" ou "Rio".
    """
    if project.project_type != "EVENT_TOPIC":
        return True

    anchor, variants, fact_variants, _, _ = _event_topic_profile(project)
    if not anchor and not variants:
        return True

    body = " ".join(filter(None, [title, snippet, content]))
    haystack = " ".join(normalized_text(body).split())
    if not haystack:
        return False

    for candidate in [anchor, *variants, *fact_variants]:
        candidate_norm = " ".join(normalized_text(candidate).split())
        if candidate_norm and candidate_norm in haystack:
            return True

    # Fallback semântico determinístico para MIAE e variantes jornalísticas.
    profile = project.topic_profile or {}
    event_type = str(profile.get("event_type") or "").upper()
    if event_type == "DEATH_BY_STATE_INTERVENTION":
        death_hit = any(
            term in haystack
            for term in ("morte", "mortes", "morto", "morta", "morre", "morreu", "obito", "letalidade")
        )
        police_action_hit = any(
            term in haystack
            for term in (
                "intervencao policial",
                "intervencao de agente do estado",
                "intervencao de agentes do estado",
                "acao policial",
                "operacao policial",
                "morto por policial",
                "morta por policial",
                "baleado por policial",
                "baleada por policial",
                "policia matou",
                "letalidade policial",
            )
        )
        return bool(death_hit and police_action_hit)

    return False


def collection_guard(
    project: Project,
    *,
    title: str | None,
    snippet: str | None = None,
    content: str | None = None,
) -> bool:
    if not _institutional_collection_guard(project, title=title, snippet=snippet, content=content):
        return False
    if not _event_collection_guard(project, title=title, snippet=snippet, content=content):
        return False
    return True



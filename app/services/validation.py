from __future__ import annotations

import math
import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.models import Classification, MediaItem, Project
from app.schemas import MediaRelevanceBatchResponse, YouTubeCrossValidationResponse
from app.source_registry import ISP_INSTITUTION_NAME
from app.topic_profile import (
    GENERIC_PRODUCT_TERMS,
    normalized_terms,
    normalized_text,
    product_anchor_from_name,
)
from app.services.collection.common import inferred_publication_date, media_window
from app.services.collection.guards import (
    collection_guard,
    institutional_product_version_guard_text,
)
from app.services.collection.youtube_helpers import is_youtube_host


def validate_video_metadata_cross_source(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict[str, int | bool]:
    """Compara metadados do mesmo video obtidos por dois coletores independentes."""
    if not llm_is_configured():
        return {"validated": 0, "skipped": True}


    candidates = []
    for item in db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all():
        sources = {row.get("source"): row for row in (item.source_provenance or [])}
        secondary_name = None
        if "duckduckgo_video" in sources:
            secondary_name = "duckduckgo_video"
        if "tavily" in sources and secondary_name:
            candidates.append((item, sources["tavily"], sources[secondary_name], secondary_name))

    if not candidates:
        return {
            "validated": 0,
            "skipped": True,
            "reason": "NO_COMPARABLE_ITEMS",
        }

    settings = get_settings()
    validation_cap = max(1, settings.max_cross_validations)
    validated = 0
    for index, (item, tavily, secondary, secondary_name) in enumerate(candidates, start=1):
        if validated >= validation_cap:
            break
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Video {index}/{len(candidates)}: {item.title[:90]}")
        result = get_report_agent().run(
            task="cross_validation",
            payload={
                "canonical_url": item.canonical_url,
                "tavily": tavily,
                "secondary_provider": secondary_name,
                "secondary": secondary,
            },
            schema_name="youtube_cross_validation",
            response_model=YouTubeCrossValidationResponse,
            max_output_tokens=700,
        )
        item.cross_validation_status = result["status"]
        item.cross_validation_detail = result["detail"][:1000]
        validated += 1
    db.commit()
    return {"validated": validated, "skipped": False, "reason": None}


def _topic_overlap_score(project: Project, body: str) -> int:
    profile = project.topic_profile or {}
    haystack = normalized_text(body)
    groups = [profile.get("actors") or [], profile.get("actions") or [], profile.get("locations") or []]
    score = 0
    for group in groups:
        if group and any(normalized_text(term) in haystack for term in group):
            score += 1
    if score == 0:
        topic_terms = normalized_terms(project.topic)
        body_terms = normalized_terms(body)
        overlap = topic_terms.intersection(body_terms)
        score = 1 if len(overlap) >= max(1, min(3, len(topic_terms) // 3)) else 0
    return score


def _institutional_anchor_score(project: Project, body: str) -> int:
    """Heurística conservadora para produto institucional quando o LLM falha.

    Tema semelhante não basta. Exigimos referência ao produto ou à instituição
    combinada com termos distintivos do produto.
    """
    haystack = normalized_text(body)
    topic = normalized_text(project.topic)
    topic_without_year = product_anchor_from_name(topic) or topic
    score = 0

    if topic and topic in haystack:
        score += 3
    elif len(topic_without_year) >= 8 and topic_without_year in haystack:
        score += 2

    institution_terms = {
        normalized_text(project.institution or ""),
        normalized_text(ISP_INSTITUTION_NAME),
    }
    profile = project.topic_profile or {}
    institution_terms.update(normalized_text(x) for x in (profile.get("organizations") or []) if x)
    institution_hit = any(term and len(term) >= 5 and term in haystack for term in institution_terms)
    acronym_hit = bool(re.search(r"\bisp(?:-rj)?\b", haystack))
    if institution_hit or acronym_hit:
        score += 1

    core_terms = {term for term in normalized_terms(project.topic) if term not in GENERIC_PRODUCT_TERMS}
    body_terms = normalized_terms(body)
    if len(core_terms.intersection(body_terms)) >= 2:
        score += 1
    return score


def _relevance_fallback_decision(project: Project, item: MediaItem) -> tuple[bool, str]:
    body = " ".join(filter(None, [item.title, item.snippet, item.content or ""]))
    fallback_score = _topic_overlap_score(project, body)
    institutional_score = (
        _institutional_anchor_score(project, body)
        if project.project_type == "INSTITUTIONAL_PRODUCT"
        else 0
    )
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        related = institutional_score >= 2
        return (
            related,
            "Âncora documental/institucional suficiente"
            if related
            else "Tema semelhante, mas sem vínculo documental suficiente com o produto institucional",
        )
    return (
        fallback_score > 0,
        "Aderência lexical" if fallback_score > 0 else "Sem aderência lexical suficiente",
    )


def _review_media_relevance_batch(
    project: Project,
    items: list[MediaItem],
) -> tuple[dict[int, tuple[bool, str]], int]:
    """Revisa vários itens em uma única chamada estruturada.

    Retorna ``({media_item_id: (related, evidence)}, llm_calls)``. Se a OpenAI
    estiver indisponível, a decisão cai para a heurística conservadora sem
    abrir uma chamada por item.
    """
    if not items:
        return {}, 0

    settings = get_settings()
    if not llm_is_configured():
        return {
            item.id: _relevance_fallback_decision(project, item)
            for item in items
        }, 0

    extra_instructions = """
Você receberá vários itens na mesma chamada. Avalie CADA item de forma independente.
Não deixe a evidência de um item contaminar a decisão de outro. Preserve exatamente o
media_item_id recebido e retorne uma decisão para cada item. Não invente conteúdo ausente.
"""
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        extra_instructions += """

REGRA OBRIGATÓRIA PARA PRODUTO INSTITUCIONAL:
O objeto desta análise é um produto institucional específico e, quando houver ano/edição no nome, essa edição faz parte da identidade do produto.

Um item só pode ter related=true quando houver vínculo documental verificável com O PRODUTO SOLICITADO.

Aceite somente:
1. DIRECT_PRODUCT: o item menciona explicitamente o produto solicitado ou sua edição correta; ou
2. ATTRIBUTED_FINDING: o item apresenta dado, conclusão, estatística ou achado e atribui explicitamente esse conteúdo ao produto solicitado ou ao ISP em contexto inequívoco da mesma edição.

REJEITE:
- outra edição do produto;
- Dossiê Mulher 2025 quando o objeto for Dossiê Mulher 2026;
- Dossiê Mulher 2024 quando o objeto for Dossiê Mulher 2026;
- matéria genérica sobre violência contra a mulher;
- caso individual de violência sem relação documental com o produto;
- matéria que apenas menciona ISP sem atribuir ao produto analisado o dado ou conclusão;
- conteúdo que compartilha apenas palavras ou temas do produto.

Uma edição anterior só pode ser aceita em uma matéria comparativa quando a edição alvo também estiver explicitamente presente e for parte material da análise.
THEMATIC_ONLY sempre implica related=false.
Não presuma que uma matéria pertence ao Dossiê apenas porque aborda violência contra mulheres no Rio de Janeiro.
O campo anchor deve identificar a evidência textual concreta que conecta o item ao produto e à edição solicitada.
"""

    payload_items = []
    max_chars = max(500, settings.validation_item_max_chars)
    for item in items:
        payload_items.append(
            {
                "media_item_id": item.id,
                "title": item.title,
                "snippet": item.snippet,
                "content": (item.content or "")[:max_chars],
                "source": item.source_name or item.domain,
                "url": item.url,
            }
        )

    try:
        result = get_report_agent().run(
            task="media_relevance",
            extra_instructions=extra_instructions,
            payload={
                "topic": project.topic,
                "project_type": project.project_type,
                "institution": project.institution,
                "product_name": (project.topic_profile or {}).get("product_name"),
                "product_anchor": (project.topic_profile or {}).get("product_anchor"),
                "product_search_variants": (project.topic_profile or {}).get("product_search_variants", []),
                "topic_profile": project.topic_profile,
                "items": payload_items,
            },
            schema_name="media_relevance_batch_v1",
            response_model=MediaRelevanceBatchResponse,
            max_output_tokens=max(2200, min(9000, 700 * len(items))),
        )
    except RuntimeError:
        return {
            item.id: _relevance_fallback_decision(project, item)
            for item in items
        }, 1

    by_id = {
        int(row["media_item_id"]): row
        for row in result.get("decisions", [])
        if row.get("media_item_id") is not None
    }
    allowed_types = (
        {"DIRECT_PRODUCT", "ATTRIBUTED_FINDING"}
        if project.project_type == "INSTITUTIONAL_PRODUCT"
        else {"DIRECT_PRODUCT", "ATTRIBUTED_FINDING", "DIRECT_EVENT"}
    )

    decisions: dict[int, tuple[bool, str]] = {}
    for item in items:
        row = by_id.get(item.id)
        if not row:
            decisions[item.id] = _relevance_fallback_decision(project, item)
            continue
        related = bool(row.get("related")) and row.get("relation_type") in allowed_types
        evidence = (
            row.get("anchor")
            or row.get("evidence")
            or row.get("reason")
            or "Sem âncora temática"
        )
        decisions[item.id] = (related, str(evidence))
    return decisions, 1


def _mentions_institution(project: Project, body: str) -> bool:
    text = normalized_text(body)
    full_name = normalized_text(project.institution or "")
    return bool(
        (full_name and full_name in text)
        or normalized_text(ISP_INSTITUTION_NAME) in text
        or re.search(r"\bisp(?:-rj)?\b", text)
    )


def low_information_title(title: str | None) -> bool:
    text = " ".join((title or "").split()).strip()
    if not text:
        return True
    normalized = normalized_text(text)
    if re.fullmatch(r"\d{1,2}\s+de\s+[a-z]+\s+de\s+20\d{2}", normalized):
        return True
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", normalized):
        return True
    return len(normalized_terms(text)) < 2


def validate_and_classify(
    db: Session,
    project: Project,
    *,
    cancel_check: Callable[[], None] | None = None,
    progress_detail: Callable[[str], None] | None = None,
) -> dict:
    settings = get_settings()
    review_cap = max(1, settings.max_semantic_reviews)
    batch_size = max(1, settings.validation_batch_size)
    use_llm = llm_is_configured()

    items = db.scalars(select(MediaItem).where(MediaItem.project_id == project.id)).all()
    start, end = media_window(project)
    enforce_window = bool(project.has_custom_date_window and start and end)

    valid = 0
    discarded = 0
    semantic_reviews = 0
    llm_calls = 0
    already_decided = 0
    pending_after_quota = 0
    candidates: list[MediaItem] = []

    total_items = len(items)
    for item_index, item in enumerate(items, start=1):
        if cancel_check:
            cancel_check()
        if progress_detail:
            progress_detail(f"Pré-triagem {item_index}/{total_items}: {item.title[:90]}")

        if item.status != "PENDING":
            already_decided += 1
            continue

        publication_date = inferred_publication_date(item)
        is_social = (
            item.search_source in {"youtube", "youtube_api", "duckduckgo_video"}
            or is_youtube_host(item.domain or "")
        )

        if enforce_window and is_social and not publication_date:
            item.status = "DATE_UNVERIFIED"
            item.discard_reason = (
                "Data de publicação do vídeo não verificável dentro de uma pauta com janela temporal"
            )
            discarded += 1
            continue

        if enforce_window and publication_date and not start <= publication_date <= end:
            item.status = "OUTSIDE_COLLECTION_WINDOW"
            item.discard_reason = (
                f"Publicado em {publication_date.isoformat()}, fora da janela midiática "
                f"{start.isoformat()} a {end.isoformat()}"
            )
            discarded += 1
            continue

        # Produto institucional com edição/ano: rejeita explicitamente a edição
        # errada antes de qualquer chamada semântica.
        if project.project_type == "INSTITUTIONAL_PRODUCT":
            version_ok, version_reason = institutional_product_version_guard_text(
                project,
                title=item.title,
                snippet=item.snippet,
                content=(item.content or "")[: settings.validation_item_max_chars],
            )
            if not version_ok:
                item.status = "NOT_RELATED"
                item.discard_reason = (version_reason or "Edição incompatível")[:1000]
                discarded += 1
                if progress_detail:
                    progress_detail(
                        f"Descartado antes da IA: {item.title[:80]} · edição incompatível"
                    )
                continue

        # Produtos institucionais e eventos factuais ancorados recebem um
        # pré-filtro determinístico antes da chamada LLM. Isso elimina ruído
        # grosseiro sem gastar revisão semântica.
        if not collection_guard(
            project,
            title=item.title,
            snippet=item.snippet,
            content=(item.content or "")[: settings.validation_item_max_chars],
        ):
            item.status = "NOT_RELATED"
            item.discard_reason = (
                "Pré-filtro temático: item não preserva a âncora nominal/factual "
                "do objeto monitorado"
            )
            discarded += 1
            continue

        candidates.append(item)

    db.commit()

    if use_llm and len(candidates) > review_cap:
        pending_after_quota = len(candidates) - review_cap
        candidates = candidates[:review_cap]

    total_batches = max(1, math.ceil(len(candidates) / batch_size)) if candidates else 0
    for batch_index, offset in enumerate(range(0, len(candidates), batch_size), start=1):
        if cancel_check:
            cancel_check()
        batch = candidates[offset : offset + batch_size]
        if progress_detail:
            progress_detail(
                f"Validação semântica em lote {batch_index}/{total_batches}: "
                f"{len(batch)} item(ns)"
            )

        decisions, calls = _review_media_relevance_batch(project, batch)
        llm_calls += calls
        semantic_reviews += len(batch)

        for item in batch:
            related, evidence = decisions.get(
                item.id,
                _relevance_fallback_decision(project, item),
            )
            if not related:
                item.status = "NOT_RELATED"
                item.discard_reason = evidence[:1000]
                discarded += 1
                continue

            item.status = "VALID"
            item.discard_reason = None
            mention = _mentions_institution(
                project,
                " ".join(filter(None, [item.title, item.snippet, item.content])),
            )
            classification = db.scalar(
                select(Classification).where(Classification.media_item_id == item.id)
            )
            if not classification:
                db.add(
                    Classification(
                        media_item_id=item.id,
                        theme="Geral",
                        framing="A determinar por revisão analítica",
                        isp_mentioned=mention,
                        tone_toward_institution="NEUTRO",
                        fidelity_status="PENDENTE",
                        evidence=(evidence or item.snippet or item.title)[:1000],
                        errors=[],
                    )
                )
            valid += 1

        db.commit()

    # Sem LLM, o cap de revisões não deve bloquear a heurística local.
    if not use_llm and candidates:
        pending_after_quota = 0

    if not project.has_custom_date_window:
        valid_items = db.scalars(
            select(MediaItem).where(
                MediaItem.project_id == project.id,
                MediaItem.status == "VALID",
            )
        ).all()
        observed_dates = [
            inferred_publication_date(item)
            for item in valid_items
            if inferred_publication_date(item) is not None
        ]
        if observed_dates:
            observed_start = min(observed_dates)
            observed_end = max(observed_dates)
            project.collection_start = observed_start
            project.collection_end = observed_end
            profile_state = dict(project.topic_profile or {})
            profile_state["observed_collection_start"] = observed_start.isoformat()
            profile_state["observed_collection_end"] = observed_end.isoformat()
            project.topic_profile = profile_state
            db.commit()

    return {
        "valid": valid,
        "discarded": discarded,
        "semantic_reviews": semantic_reviews,
        "llm_reviews": semantic_reviews if use_llm else 0,
        "llm_calls": llm_calls,
        "batch_size": batch_size,
        "batches": total_batches,
        "already_decided": already_decided,
        "pending_after_quota": pending_after_quota,
        "temporal_mode": "explicit_window" if enforce_window else "topic_driven",
    }



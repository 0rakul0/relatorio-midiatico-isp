from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import cost_context
from app.llm import llm_is_configured
from app.models import Project, PublicOpinionSurvey
from app.schemas import PublicOpinionSurveyExtractionResponse
from app.services.collection.common import result_publication_date
from app.tools.public_opinion import (
    discover_public_opinion_sources,
    hydrate_public_opinion_source,
)


METHODOLOGY_NOTE = (
    "A camada de opiniao publica usa levantamentos identificados como pesquisas "
    "com populacao-alvo, amostra e metodologia declaradas. Resultados devem ser "
    "interpretados dentro do universo pesquisado, do periodo de campo e das "
    "limitacoes metodologicas de cada estudo. Comentarios de redes sociais nao "
    "sao tratados como pesquisa de opiniao."
)


def _clean(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _as_date(value: object) -> date | None:
    parsed = result_publication_date(value)
    if parsed is not None:
        return parsed
    text = _clean(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _search_terms(project: Project) -> list[str]:
    profile = project.topic_profile or {}
    base_terms = [
        *(profile.get("search_synonyms") or []),
        *(profile.get("subject_terms") or []),
    ]
    topic = _clean(project.topic) or ""
    anchors: list[str] = []
    for raw in [*base_terms, topic]:
        term = _clean(raw)
        if not term:
            continue
        words = term.split()
        if len(words) > 9:
            term = " ".join(words[:9])
        if term.casefold() not in {x.casefold() for x in anchors}:
            anchors.append(term)
        if len(anchors) >= 3:
            break
    return anchors or [topic]


def _queries(project: Project) -> list[str]:
    settings = get_settings()
    queries: list[str] = []
    suffixes = (
        '"pesquisa de opinião" população amostra metodologia',
        '"pesquisa" opinião pública amostra margem de erro',
        '"levantamento" população entrevistados metodologia',
    )
    for term in _search_terms(project):
        for suffix in suffixes:
            query = f"{term} {suffix}".strip()
            if query.casefold() not in {x.casefold() for x in queries}:
                queries.append(query)
            if len(queries) >= int(settings.public_opinion_max_queries):
                return queries
    return queries


def _survey_row(row: PublicOpinionSurvey) -> dict:
    return {
        "id": row.id,
        "institute": row.institute,
        "sponsor": row.sponsor,
        "population": row.population,
        "geography": row.geography,
        "field_start": row.field_start.isoformat() if row.field_start else None,
        "field_end": row.field_end.isoformat() if row.field_end else None,
        "publication_date": row.publication_date.isoformat() if row.publication_date else None,
        "sample_size": row.sample_size,
        "margin_of_error": row.margin_of_error,
        "confidence_level": row.confidence_level,
        "methodology": row.methodology,
        "sampling_method": row.sampling_method,
        "representative_scope": row.representative_scope,
        "caveats": row.caveats,
        "indicators": list(row.indicators or []),
        "evidence_summary": row.evidence_summary,
        "source_title": row.source_title,
        "source_url": row.source_url,
        "source_domain": row.source_domain,
        "provider": row.provider,
    }


def public_opinion_for_report(db: Session, project_id: int) -> dict:
    rows = list(
        db.scalars(
            select(PublicOpinionSurvey)
            .where(PublicOpinionSurvey.project_id == project_id)
            .order_by(
                PublicOpinionSurvey.field_end.desc(),
                PublicOpinionSurvey.publication_date.desc(),
                PublicOpinionSurvey.id.asc(),
            )
        ).all()
    )
    return {
        "status": "COMPLETED" if rows else "NO_SURVEYS",
        "surveys": [_survey_row(row) for row in rows],
        "count": len(rows),
        "methodology_note": METHODOLOGY_NOTE,
    }


def collect_public_opinion(db: Session, project: Project) -> dict:
    settings = get_settings()
    if not settings.public_opinion_enabled:
        return {
            "status": "DISABLED",
            "queries": [],
            "candidates": 0,
            "surveys": [],
            "count": 0,
            "methodology_note": METHODOLOGY_NOTE,
        }

    queries = _queries(project)
    discovered = discover_public_opinion_sources(
        queries=queries,
        results_per_query=int(settings.public_opinion_results_per_query),
    )
    candidates = {
        str(row.get("url") or "").strip(): row
        for row in discovered
        if str(row.get("url") or "").strip()
    }

    if not candidates:
        return {
            "status": "NO_SURVEYS",
            "queries": queries,
            "candidates": 0,
            "surveys": [],
            "count": 0,
            "methodology_note": METHODOLOGY_NOTE,
        }

    if not llm_is_configured():
        return {
            "status": "DISCOVERED_ONLY",
            "queries": queries,
            "candidates": len(candidates),
            "surveys": [],
            "count": 0,
            "methodology_note": METHODOLOGY_NOTE,
        }

    # Reprocessamos a camada para o projeto corrente para evitar manter uma
    # extracao antiga quando as regras/schema forem refinados.
    db.execute(delete(PublicOpinionSurvey).where(PublicOpinionSurvey.project_id == project.id))
    db.flush()

    selected = list(candidates.values())[: int(settings.public_opinion_max_documents)]
    persisted = 0
    for row in selected:
        url = str(row.get("url") or "").strip()
        content = hydrate_public_opinion_source(
            url,
            max_chars=int(settings.public_opinion_fetch_max_chars),
            timeout_seconds=float(settings.public_opinion_fetch_timeout_seconds),
        )
        text = content or _clean(row.get("snippet")) or ""
        if len(text) < 80:
            continue

        payload = {
            "project": {
                "topic": project.topic,
                "collection_start": project.collection_start.isoformat(),
                "collection_end": project.collection_end.isoformat(),
            },
            "source": {
                "title": row.get("title"),
                "url": url,
                "published_at": row.get("published_at"),
                "snippet": row.get("snippet"),
                "content": text[: int(settings.public_opinion_llm_max_chars)],
            },
            "methodology": METHODOLOGY_NOTE,
        }
        try:
            with cost_context(
                project_id=project.id,
                operation="public_opinion_extraction",
                schema_name="public_opinion_survey_extraction_v1",
            ):
                result = get_report_agent().run(
                    task="public_opinion_extraction",
                    payload=payload,
                    response_model=PublicOpinionSurveyExtractionResponse,
                    schema_name="public_opinion_survey_extraction_v1",
                    max_output_tokens=4500,
                )
        except RuntimeError:
            continue

        if not result.get("is_public_opinion_research"):
            continue
        indicators = list(result.get("indicators") or [])
        if not indicators:
            continue

        survey = PublicOpinionSurvey(
            project_id=project.id,
            institute=_clean(result.get("institute")) or "Instituto não identificado",
            sponsor=_clean(result.get("sponsor")),
            population=_clean(result.get("population")),
            geography=_clean(result.get("geography")),
            field_start=_as_date(result.get("field_start")),
            field_end=_as_date(result.get("field_end")),
            publication_date=_as_date(result.get("publication_date"))
            or result_publication_date(row.get("published_at")),
            sample_size=result.get("sample_size"),
            margin_of_error=_clean(result.get("margin_of_error")),
            confidence_level=_clean(result.get("confidence_level")),
            methodology=_clean(result.get("methodology")),
            sampling_method=_clean(result.get("sampling_method")),
            representative_scope=_clean(result.get("representative_scope")),
            caveats=_clean(result.get("caveats")),
            indicators=indicators,
            evidence_summary=_clean(result.get("evidence_summary")),
            source_title=_clean(row.get("title")) or "Pesquisa de opinião",
            source_url=url,
            source_domain=urlparse(url).netloc.lower(),
            provider=str(row.get("provider") or "duckduckgo_text"),
        )
        db.add(survey)
        persisted += 1

    db.commit()
    report = public_opinion_for_report(db, project.id)
    report["queries"] = queries
    report["candidates"] = len(candidates)
    report["documents_analyzed"] = len(selected)
    report["persisted"] = persisted
    return report

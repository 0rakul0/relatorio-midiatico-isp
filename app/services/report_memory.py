"""Memoria editorial: recupera leituras anteriores, nunca como fonte factual.

Reutiliza snapshots imutaveis ja aprovados em report_versions. A comparacao
e restrita ao mesmo proprietario/tema/edicao, com proveniencia explicita,
deduplicacao de URLs e cautela para janelas de coleta diferentes.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Project, ReportVersion
from app.topic_profile import normalized_text


_STOP = frozenset({
    "a", "as", "o", "os", "de", "do", "da", "dos", "das", "em", "no", "na",
    "nos", "nas", "para", "por", "com", "e", "um", "uma", "sobre", "tema",
    "relacionado", "relacionada", "relacionados", "repercussao", "midiatica",
    "monitoramento", "cobertura", "analise", "análise", "estado", "ano",
})
_IGNORED_QUERY = frozenset({"fbclid", "gclid", "ref", "source"})


def _tokens(topic: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized_text(topic or ""))
        if token not in _STOP and len(token) >= 2
    }


def same_topic(current: Project, previous: Project) -> bool:
    """Compatibilidade conservadora; anos distintos nunca sao mesmo produto."""
    current_type = str(current.project_type or "").upper()
    previous_type = str(previous.project_type or "").upper()
    if current_type != previous_type and "AUTO" not in {current_type, previous_type}:
        return False
    now = _tokens(current.topic)
    before = _tokens(previous.topic)
    if not now or not before:
        return False
    years_now = {token for token in now if re.fullmatch(r"20\d\d", token)}
    years_before = {token for token in before if re.fullmatch(r"20\d\d", token)}
    if years_now != years_before:
        return False
    # Ex.: "Repercussao do Dossie Mulher 2026" e "Dossie Mulher 2026".
    return len(now & before) / len(now | before) >= 0.80


def _url_key(value: object) -> str | None:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query)
        if not key.lower().startswith("utm_") and key.lower() not in _IGNORED_QUERY
    ))
    return urlunsplit((
        "https",
        parsed.netloc.casefold().removeprefix("www."),
        parsed.path.rstrip("/"),
        query,
        "",
    ))


def _corpus_index(payload: dict) -> dict[str, dict]:
    result = {}
    for item in payload.get("corpus") or []:
        if not isinstance(item, dict):
            continue
        key = _url_key(item.get("url"))
        if key:
            result[key] = {
                "title": str(item.get("title") or "")[:180],
                "url": str(item.get("url") or ""),
            }
    return result


def _window(project: Project) -> dict:
    return {
        "explicit": bool(project.has_custom_date_window),
        "start": project.collection_start.isoformat() if project.collection_start else None,
        "end": project.collection_end.isoformat() if project.collection_end else None,
    }


def _summary(payload: dict) -> dict:
    report = payload.get("report") or {}
    metrics = payload.get("metrics") or {}
    return {
        "executive_summary": str(report.get("executive_summary") or "")[:1000],
        "dominant_framing": str(report.get("dominant_framing") or "")[:1500],
        "institutional_narrative": str(report.get("institutional_narrative") or "")[:850],
        "methodological_note": str(report.get("methodological_note") or "")[:900],
        "risk_assessment": [
            {
                "dimension": str(row.get("dimension") or "")[:100],
                "assessment": str(row.get("assessment") or "")[:250],
            }
            for row in (report.get("risk_assessment") or [])[:6]
            if isinstance(row, dict)
        ],
        "thematic_axes": [
            {
                "axis": str(axis.get("axis") or "")[:120],
                "coverage": str(axis.get("coverage") or "")[:350],
            }
            for axis in (report.get("thematic_axes") or [])[:8]
            if isinstance(axis, dict)
        ],
        "metrics": {
            "valid_items": int(metrics.get("valid_items") or 0),
            "unique_vehicles": int(metrics.get("unique_vehicles") or 0),
            "media_origin_counts": metrics.get("media_origin_counts") or {},
        },
    }


def previous_analyses(
    db: Session,
    project: Project,
    current_corpus: list[dict],
    *,
    limit: int = 3,
) -> dict:
    """Consulta somente versoes APROVADAS anteriores do mesmo titular.

    Para projetos legados sem owner, a memoria fica limitada ao proprio
    projeto, evitando leitura cruzada de informacoes privadas.
    """
    project_stmt = select(Project).order_by(Project.id.desc()).limit(1000)
    if project.owner_id:
        project_stmt = project_stmt.where(Project.owner_id == project.owner_id)
    else:
        project_stmt = project_stmt.where(Project.id == project.id)
    candidates = {
        row.id: row for row in db.scalars(project_stmt).all()
        if same_topic(project, row)
    }
    if not candidates:
        return {"status": "NO_PRIOR_APPROVED_REPORT", "prior_reports": []}

    # Filtra primeiro projetos leves; so depois carrega payloads historicos
    # potencialmente grandes. Nao traz centenas de outros temas para memoria.
    stmt = (
        select(ReportVersion)
        .where(
            ReportVersion.qa_status == "APPROVED",
            ReportVersion.project_id.in_(list(candidates)),
        )
        .order_by(ReportVersion.created_at.desc(), ReportVersion.id.desc())
        .limit(max(1, min(limit, 5)))
    )
    matches: list[dict] = []
    for version in db.scalars(stmt).all():
        old_project = candidates[version.project_id]
        body = version.body
        if not isinstance(body, dict) or not isinstance(body.get("report"), dict):
            continue
        prior_window = _window(old_project)
        current_window = _window(project)
        comparable = (
            prior_window["explicit"] and current_window["explicit"]
            and prior_window["start"] == current_window["start"]
            and prior_window["end"] == current_window["end"]
        )
        matches.append({
            "project_id": old_project.id,
            "report_version_id": version.id,
            "version_no": version.version_no,
            "topic": old_project.topic,
            "created_at": version.created_at.isoformat() if version.created_at else None,
            "prior_window": prior_window,
            "current_window": current_window,
            "same_explicit_window": comparable,
            "reading": _summary(body),
            "_corpus_index": _corpus_index(body),
        })
        if len(matches) >= max(1, min(limit, 5)):
            break
    if not matches:
        return {"status": "NO_PRIOR_APPROVED_REPORT", "prior_reports": []}

    latest = matches[0]
    current_urls = _corpus_index({"corpus": current_corpus})
    prior_urls = latest.pop("_corpus_index")
    new_keys = current_urls.keys() - prior_urls.keys()
    missing_keys = prior_urls.keys() - current_urls.keys()
    shared_keys = current_urls.keys() & prior_urls.keys()
    for old in matches[1:]:
        old.pop("_corpus_index")

    return {
        "status": "AVAILABLE",
        "prior_reports": matches,
        "comparison_basis": {
            "baseline_report_version_id": latest["report_version_id"],
            "same_explicit_window": latest["same_explicit_window"],
            "current_corpus_items_with_url": len(current_urls),
            "previous_corpus_items_with_url": len(prior_urls),
            "shared_document_count": len(shared_keys),
            "new_to_current_sample_count": len(new_keys),
            "absent_from_current_sample_count": len(missing_keys),
            "examples_new_to_sample": [
                current_urls[key] for key in sorted(new_keys)[:5]
            ],
            "interpretation": (
                "Contagens refletem apenas amostras documentais e janelas explicitas iguais; "
                "nao representam tendencia na imprensa."
                if latest["same_explicit_window"]
                else "Janelas de amostragem nao sao diretamente comparaveis. "
                     "Nao calcule tendencia percentual, crescimento de cobertura "
                     "ou variacao de opiniao publica."
            ),
        },
    }

"""Refinamento de relatórios históricos guiado por achados bloqueadores do QA.

A rotina preserva o projeto/corpus existente e abre apenas buscas corretivas
quando um achado CRITICAL/HIGH depende de nova evidência. Depois revalida,
reconsolida fatos/operações, reclassifica, reescreve e executa o QA novamente.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.fact_layer import extract_project_facts, resolve_project_facts
from app.models import GeneratedReport, Project, SearchQuery
from app.report_qa import run_report_qa
from app.services.cache import hydrate_cached_report
from app.services.classification import classify_with_llm
from app.services.collection.web import collect_web
from app.services.execution_profile import execution_flags
from app.services.news_validation import validate_news_stage
from app.services.reporting import draft_report_with_llm, refine_report_with_qa
from app.source_registry import OFFICIAL_OPERATION_INVENTORY_SOURCES
from app.topic_profile import normalized_text


MAX_QA_RESEARCH_ROUNDS = 3
MAX_QA_RESEARCH_QUERIES_PER_ROUND = 4


def _blocking_findings(qa: dict) -> list[dict]:
    return [
        finding
        for finding in qa.get("findings", [])
        if str(finding.get("severity") or "").upper() in {"CRITICAL", "HIGH"}
    ]


def _research_focus(findings: list[dict]) -> str | None:
    """Converte códigos de QA em um foco factual curto e auditável."""
    haystack = normalized_text(
        " ".join(
            f"{item.get('code', '')} {item.get('message', '')}"
            for item in findings
        )
    )
    if any(token in haystack for token in ("mortal", "mortos", "obitos", "death")):
        return "mortos balanço oficial atualização operação"
    if any(token in haystack for token in ("fuzil", "arma", "weapon", "apreens")):
        return "armas fuzis apreendidos balanço oficial operação"
    if any(token in haystack for token in ("presos", "pris", "arrest")):
        return "presos balanço oficial operação"
    if any(token in haystack for token in ("numeric", "cifra", "balanco", "contagem")):
        return "balanço oficial operação"
    if any(token in haystack for token in ("fonte", "evidencia", "confirm")):
        return "fonte oficial confirmação operação"
    return None


def _plan_corrective_queries(
    db: Session,
    project: Project,
    findings: list[dict],
) -> list[SearchQuery]:
    """Gera poucas consultas oficiais novas sem repetir o histórico."""
    focus = _research_focus(findings)
    if not focus:
        return []

    existing = {
        " ".join(str(value or "").split()).strip().casefold()
        for value in db.scalars(
            select(SearchQuery.query).where(SearchQuery.project_id == project.id)
        ).all()
    }

    # Para problemas factuais, priorizamos PCERJ, PMERJ e Governo do Estado.
    preferred = [
        source
        for source in OFFICIAL_OPERATION_INVENTORY_SOURCES
        if source.get("domain")
    ]
    candidates: list[tuple[str, str]] = []
    for source in preferred:
        query = (
            f'site:{source["domain"]} "{project.topic}" {focus}'
        )
        candidates.append(
            (
                query,
                f'Refinamento QA em fonte oficial: {source["label"]}.',
            )
        )

    # Uma busca aberta complementar ajuda quando a fonte oficial foi
    # repercutida por veículos mas o release original não está bem indexado.
    candidates.append(
        (
            f'"{project.topic}" {focus}',
            "Refinamento QA em busca aberta para localizar a origem/atualização do balanço.",
        )
    )

    created: list[SearchQuery] = []
    for query, rationale in candidates[:MAX_QA_RESEARCH_QUERIES_PER_ROUND]:
        compact = " ".join(query.split()).strip()
        if compact.casefold() in existing:
            continue
        row = SearchQuery(
            project_id=project.id,
            query=compact,
            kind="qa_refinement",
            purpose=(
                "OFFICIAL_FACT"
                if compact.casefold().startswith("site:")
                else "FACT_DISCOVERY"
            ),
            rationale=rationale,
            priority=1,
        )
        db.add(row)
        created.append(row)
        existing.add(compact.casefold())

    db.commit()
    return created


def refine_historical_report_until_qa(
    db: Session,
    project: Project,
    *,
    progress=None,
    cancel_check=None,
) -> dict:
    saved = db.scalar(
        select(GeneratedReport).where(GeneratedReport.project_id == project.id)
    )
    if not saved or not saved.body:
        raise RuntimeError("O projeto ainda não possui relatório para refinar")

    _profile, flags = execution_flags(project)
    payload = hydrate_cached_report(db, project, saved)
    qa = run_report_qa(db, project, payload)
    rounds: list[dict] = []

    for round_no in range(1, MAX_QA_RESEARCH_ROUNDS + 1):
        if cancel_check:
            cancel_check()
        if qa.get("approved"):
            break

        blocking = _blocking_findings(qa)
        if not blocking:
            break

        if progress:
            progress(
                "gap_fill",
                "RUNNING",
                f"Rodada QA {round_no}: analisando {len(blocking)} bloqueio(s)",
            )

        queries = _plan_corrective_queries(db, project, blocking)
        collected = 0

        if queries:
            if progress:
                progress(
                    "gap_fill",
                    "RUNNING",
                    f"Rodada QA {round_no}: executando {len(queries)} busca(s) corretiva(s)",
                )
            collected = collect_web(
                db,
                project.id,
                cancel_check=cancel_check,
                progress_detail=(
                    (lambda detail: progress("gap_fill", "RUNNING", detail))
                    if progress else None
                ),
            )

            if progress:
                progress("validation", "RUNNING", "Revalidando corpus após pesquisa corretiva")
            validation = validate_news_stage(
                db,
                project,
                cancel_check=cancel_check,
                progress_detail=(
                    (lambda detail: progress("validation", "RUNNING", detail))
                    if progress else None
                ),
            )
            if progress:
                progress(
                    "validation",
                    "DONE",
                    f"{validation.get('valid', 0)} item(ns) válido(s) após refinamento",
                )

            if flags.get("enable_fact_layer"):
                if progress:
                    progress("facts_pass_2", "RUNNING", "Extraindo fatos das novas evidências")
                extract_project_facts(
                    db,
                    project,
                    cancel_check=cancel_check,
                    progress_detail=(
                        (lambda detail: progress("facts_pass_2", "RUNNING", detail))
                        if progress else None
                    ),
                )
                if progress:
                    progress("fact_resolution_2", "RUNNING", "Reconciliando fatos e tabela-mestra de operações")
                resolution = resolve_project_facts(db, project)
                if progress:
                    progress("facts_pass_2", "DONE", "Extração factual corretiva concluída")
                    progress(
                        "fact_resolution_2",
                        "DONE",
                        f"{resolution.get('confirmed', 0)} confirmado(s); "
                        f"{resolution.get('conflicts', 0)} conflito(s)",
                    )

            if flags.get("enable_classification"):
                if progress:
                    progress("classification", "RUNNING", "Classificando novos itens")
                classification = classify_with_llm(
                    db,
                    project,
                    cancel_check=cancel_check,
                    progress_detail=(
                        (lambda detail: progress("classification", "RUNNING", detail))
                        if progress else None
                    ),
                )
                if progress:
                    progress(
                        "classification",
                        "DONE",
                        f"{classification.get('updated', 0)} item(ns) classificado(s)",
                    )

            if progress:
                progress("report", "RUNNING", "Reescrevendo relatório com as novas evidências")
            payload = draft_report_with_llm(db, project)
            if progress:
                progress("report", "DONE", "Nova versão do relatório persistida")
        else:
            if progress:
                progress(
                    "report",
                    "RUNNING",
                    "Nenhuma busca factual nova útil; revisando apenas os bloqueios narrativos",
                )
            payload, _temporary_qa, revisions = refine_report_with_qa(
                db,
                project,
                payload,
                qa,
                progress_detail=(
                    (lambda detail: progress("report", "RUNNING", detail))
                    if progress else None
                ),
            )
            if progress:
                progress("report", "DONE", f"{revisions} revisão(ões) narrativa(s) aplicada(s)")

        if cancel_check:
            cancel_check()
        if progress:
            progress("qa", "RUNNING", f"Rodada QA {round_no}: auditando nova versão")
        qa = run_report_qa(db, project, payload)
        remaining = len(_blocking_findings(qa))
        rounds.append(
            {
                "round": round_no,
                "queries_created": len(queries),
                "new_items": collected,
                "blocking_after": remaining,
                "qa_status": qa.get("status"),
            }
        )

        # Sem novas buscas e ainda rejeitado: outra rodada idêntica só
        # consumiria tokens sem acrescentar evidência.
        if not queries and not qa.get("approved"):
            break

    project.status = "REPORT_READY" if qa.get("approved") else "REPORT_NEEDS_REVIEW"
    db.commit()
    if progress:
        progress(
            "qa",
            "DONE",
            f"QA {qa.get('status')} após {len(rounds)} rodada(s) de refinamento",
        )
    return {"qa": qa, "rounds": rounds}

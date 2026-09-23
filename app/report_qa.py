from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.llm import llm_is_configured
from app.schemas import ReportQAResponse
from app.models import GeneratedReport, Project, ReportVersion


FORBIDDEN_ZERO_CORPUS_PHRASES = [
    "não houve cobertura",
    "nao houve cobertura",
    "ausência total de cobertura",
    "ausencia total de cobertura",
    "nenhum veículo publicou",
    "nenhum veiculo publicou",
    "não houve repercussão",
    "nao houve repercussao",
]

FORBIDDEN_NO_FACT_PHRASES = [
    "não houve ocorrência",
    "nao houve ocorrencia",
    "não ocorreu",
    "nao ocorreu",
]

EXECUTIVE_TECHNICAL_TERMS = [
    "youtube_collection_status",
    "youtube_collection_error",
    "execution_profile",
    "execution_flags",
    "enable_youtube",
    "enable_fact_layer",
    "enable_nominal_followup",
    "NOT_ATTEMPTED",
    "NOT_CONFIGURED",
    "UNAVAILABLE",
    "DISABLED",
    "MEDIA_REPERCUSSION",
    "FACT_DISCOVERY",
    "OFFICIAL_FACT",
    "NOMINAL_FOLLOWUP",
]


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_string_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_string_values(child))
        return result
    return []


def deterministic_report_qa(payload: dict) -> list[dict]:
    report = payload.get("report") or {}
    metrics = payload.get("metrics") or {}
    facts = payload.get("fact_events") or []
    findings: list[dict] = []
    text = " ".join(_string_values(report)).casefold()
    executive_summary = str(report.get("executive_summary") or "")

    for technical_term in EXECUTIVE_TECHNICAL_TERMS:
        if technical_term.casefold() in executive_summary.casefold():
            findings.append(
                {
                    "severity": "HIGH",
                    "code": "TECHNICAL_TERM_IN_EXECUTIVE_SUMMARY",
                    "message": (
                        f"O resumo executivo expõe o identificador técnico '{technical_term}'. "
                        "Substitua-o por linguagem editorial compreensível para público não técnico."
                    ),
                }
            )

    if metrics.get("valid_items", 0) == 0:
        for phrase in FORBIDDEN_ZERO_CORPUS_PHRASES:
            if phrase in text:
                findings.append(
                    {
                        "severity": "CRITICAL",
                        "code": "ABSENCE_OVERCLAIM",
                        "message": (
                            f"O relatório usa '{phrase}' apesar de haver apenas zero itens "
                            "validados na amostra. Use 'nenhum item validado na amostra'."
                        ),
                    }
                )

    if not facts:
        for phrase in FORBIDDEN_NO_FACT_PHRASES:
            if phrase in text:
                findings.append(
                    {
                        "severity": "CRITICAL",
                        "code": "FACT_ABSENCE_OVERCLAIM",
                        "message": (
                            f"O relatório usa '{phrase}' sem uma camada factual suficiente. "
                            "Ausência na amostra não prova inexistência do fato."
                        ),
                    }
                )

    for event in facts:
        exclusion_reason = event.get("report_exclusion_reason")
        if exclusion_reason:
            findings.append(
                {
                    "severity": "CRITICAL",
                    "code": "EXCLUDED_FACT_IN_MAIN_REPORT",
                    "message": (
                        f"Evento {event.get('id')} foi enviado ao relatório principal apesar de estar excluído: "
                        f"{exclusion_reason}."
                    ),
                }
            )
        conflicts = event.get("conflict_fields") or []
        if event.get("resolution_status") == "SOURCE_CONFLICT" and not conflicts:
            findings.append(
                {
                    "severity": "HIGH",
                    "code": "CONFLICT_WITHOUT_FIELDS",
                    "message": f"Evento {event.get('id')} está em conflito, mas não registra os campos conflitantes.",
                }
            )

    operation_inventory = metrics.get("operation_inventory") or {}
    months_expected = int(operation_inventory.get("months_expected") or 0)
    if months_expected >= 6:
        missing_operations = list(operation_inventory.get("months_missing_operations") or [])
        missing_official = list(operation_inventory.get("months_missing_official_support") or [])
        if missing_operations:
            findings.append({
                "severity": "MEDIUM",
                "code": "OPERATION_INVENTORY_MONTH_GAPS",
                "message": (
                    f"Cobertura factual do inventário: {months_expected - len(missing_operations)}/{months_expected} mês(es) "
                    f"com pelo menos uma operação identificada. Sem operação identificada na amostra em: "
                    f"{', '.join(missing_operations)}. Isso não prova ausência de operações nesses meses."
                ),
            })
        elif missing_official:
            findings.append({
                "severity": "LOW",
                "code": "OPERATION_INVENTORY_OFFICIAL_GAPS",
                "message": (
                    f"O inventário tem operação identificada em todos os {months_expected} meses, mas faltou "
                    f"suporte oficial localizado em: {', '.join(missing_official)}. "
                    "Ausência na amostra oficial não prova inexistência da operação."
                ),
            })

    project = payload.get("project") or {}

    if project.get("project_type") == "AUTO":
        findings.append(
            {
                "severity": "HIGH",
                "code": "UNRESOLVED_PROJECT_TYPE",
                "message": "O relatório final ainda expõe o tipo AUTO; o perfil temático deve estar resolvido antes da redação.",
            }
        )

    if metrics.get("valid_items", 0) > 0 and (not project.get("collection_start") or not project.get("collection_end")):
        findings.append(
            {
                "severity": "HIGH",
                "code": "MISSING_OBSERVED_MEDIA_WINDOW",
                "message": "Há itens validados, mas o período observado da repercussão não foi materializado no relatório.",
            }
        )

    if metrics.get("isp_mention_percent") is not None and "protagon" in text:
        findings.append(
            {
                "severity": "MEDIUM",
                "code": "MENTION_VS_PROTAGONISM",
                "message": "O texto usa linguagem de protagonismo; confirme que isso não foi inferido apenas da métrica de menção institucional.",
            }
        )

    if project.get("event_start") and project.get("event_end") and project.get("collection_start") and project.get("collection_end"):
        # Apenas sinaliza inconsistências estruturais óbvias; janelas diferentes são permitidas.
        if project["event_start"] > project["event_end"]:
            findings.append(
                {
                    "severity": "CRITICAL",
                    "code": "INVALID_EVENT_WINDOW",
                    "message": "A janela factual está invertida.",
                }
            )
        if project["collection_start"] > project["collection_end"]:
            findings.append(
                {
                    "severity": "CRITICAL",
                    "code": "INVALID_MEDIA_WINDOW",
                    "message": "A janela de repercussão está invertida.",
                }
            )

    return findings


def _llm_qa(payload: dict) -> list[dict]:
    if not llm_is_configured():
        return []
    corpus = payload.get("corpus") or []
    compact = {
        "project": payload.get("project"),
        "metrics": payload.get("metrics"),
        "fact_events": payload.get("fact_events"),
        "report": payload.get("report"),
        "corpus": [
            {
                "title": item.get("title"),
                "source": item.get("source") or item.get("domain"),
                "theme": item.get("theme"),
                "evidence": item.get("evidence"),
                "published_at": item.get("published_at"),
            }
            for item in corpus[:60]
        ],
    }
    try:
        result = get_report_agent().run(
            task="qa",
            payload=compact,
            schema_name="report_qa",
            response_model=ReportQAResponse,
            # Achados (até 30) + trilha de reasoning dos modelos gpt-5/o-series
            # precisam caber juntos no teto de completion.
            max_output_tokens=8000,
        )
    except RuntimeError as exc:
        return [
            {
                "severity": "MEDIUM",
                "code": "LLM_QA_UNAVAILABLE",
                "message": str(exc),
            }
        ]
    return list(result.get("findings", []))


def _dedupe_findings(findings: list[dict]) -> list[dict]:
    severity_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    seen: dict[tuple[str, str], dict] = {}
    for finding in findings:
        key = (str(finding.get("code")), str(finding.get("message")))
        rank = severity_rank.get(str(finding.get("severity")).upper(), 0)
        previous = seen.get(key)
        if previous is None or rank > severity_rank.get(str(previous.get("severity")).upper(), 0):
            seen[key] = finding
    return list(seen.values())


def run_report_qa(db: Session, project: Project, payload: dict) -> dict:
    from app.billing import plan_allows

    findings = deterministic_report_qa(payload)
    if get_settings().enable_llm_qa and plan_allows(db, project, "llm_qa"):
        findings.extend(_llm_qa(payload))
    else:
        findings.append(
            {
                "severity": "LOW",
                "code": "LLM_QA_DISABLED",
                "message": "Auditoria narrativa por LLM desligada (ENABLE_LLM_QA=false); vale só o QA determinístico.",
            }
        )
    findings = _dedupe_findings(findings)
    blocking = [finding for finding in findings if finding.get("severity") in {"CRITICAL", "HIGH"}]
    approved = not blocking

    saved = db.scalar(select(GeneratedReport).where(GeneratedReport.project_id == project.id))
    if saved:
        saved.qa_status = "APPROVED" if approved else "REJECTED"
        saved.qa_findings = findings
        if saved.current_version_id:
            version = db.get(ReportVersion, saved.current_version_id)
            if version:
                version.qa_status = saved.qa_status
                version.qa_findings = findings
    db.commit()
    return {
        "approved": approved,
        "status": "APPROVED" if approved else "REJECTED",
        "findings": findings,
    }

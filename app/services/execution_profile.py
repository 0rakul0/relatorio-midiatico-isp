from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.models import Project


# Explicit profiles remain user-controlled presets. AUTO is resolved by the
# report planner and persisted in Project.execution_plan.
EXECUTION_PROFILE_DEFAULTS = {
    "MIDIATICO_SIMPLES": {
        "enable_web_collection": True,
        "enable_youtube": True,
        "enable_social_repercussion": False,
        "enable_academic_research": True,
        "enable_media_validation": True,
        "enable_fact_layer": False,
        "enable_fact_resolution": False,
        "enable_nominal_followup": False,
        "enable_second_fact_pass": False,
        "enable_classification": True,
        "enable_report_writer": True,
        "enable_qa": True,
    },
    "MIDIATICO_COM_FATOS": {
        "enable_web_collection": True,
        "enable_youtube": True,
        "enable_social_repercussion": False,
        "enable_academic_research": True,
        "enable_media_validation": True,
        "enable_fact_layer": True,
        "enable_fact_resolution": True,
        "enable_nominal_followup": False,
        "enable_second_fact_pass": False,
        "enable_classification": True,
        "enable_report_writer": True,
        "enable_qa": True,
    },
    "COMPLETO_NOMINAL": {
        "enable_web_collection": True,
        "enable_youtube": True,
        "enable_social_repercussion": False,
        "enable_academic_research": True,
        "enable_media_validation": True,
        "enable_fact_layer": True,
        "enable_fact_resolution": True,
        "enable_nominal_followup": True,
        "enable_second_fact_pass": True,
        "enable_classification": True,
        "enable_report_writer": True,
        "enable_qa": True,
    },
}

_PROCESS_TO_FLAG = {
    "web_collection": "enable_web_collection",
    "youtube_collection": "enable_youtube",
    "social_repercussion": "enable_social_repercussion",
    "academic_research": "enable_academic_research",
    "media_validation": "enable_media_validation",
    "fact_extraction": "enable_fact_layer",
    "fact_resolution": "enable_fact_resolution",
    "nominal_followup": "enable_nominal_followup",
    "second_fact_pass": "enable_second_fact_pass",
    "classification": "enable_classification",
    "report_writer": "enable_report_writer",
    "qa": "enable_qa",
}


def _decision(enabled: bool, reason: str) -> dict[str, Any]:
    return {"enabled": bool(enabled), "reason": reason}


def heuristic_execution_plan(project: Project) -> dict[str, Any]:
    """Conservative fallback when the planner LLM is unavailable.

    It preserves the old behavior for event topics while avoiding nominal
    follow-up unless the topic profile explicitly asks for subject names.
    """
    profile = project.topic_profile or {}
    project_type = (project.project_type or "AUTO").upper()
    requested_fields = set(profile.get("requested_fact_fields") or [])
    event_type = str(profile.get("event_type") or "").upper()

    fact_layer = project_type == "EVENT_TOPIC"
    nominal_event_types = {"DEATH", "HOMICIDE", "FEMICIDE", "FEMINICIDE", "MURDER"}
    nominal = bool(
        fact_layer
        and "subject_name" in requested_fields
        and event_type in nominal_event_types
    )

    if project_type == "INSTITUTIONAL_PRODUCT":
        fact_layer = False
        nominal = False

    return {
        "processes": {
            "web_collection": _decision(True, "A coleta web e a base do relatorio midiatico."),
            "youtube_collection": _decision(True, "Videos podem ampliar a cobertura observada."),
            "social_repercussion": _decision(
                False,
                "Sem decisao do planejador, a coleta paga de comentarios sociais permanece desativada.",
            ),
            "academic_research": _decision(True, "Literatura cientifica pode acrescentar contexto tecnico sem integrar a metrica de repercussao."),
            "media_validation": _decision(True, "Todo corpus coletado precisa ser validado antes da analise."),
            "fact_extraction": _decision(fact_layer, "Camada factual ativada para pauta de eventos." if fact_layer else "A pauta nao exige fatos individuais estruturados."),
            "fact_resolution": _decision(fact_layer, "Consolida evidencias da camada factual." if fact_layer else "Sem extracao factual, nao ha consolidacao factual."),
            "nominal_followup": _decision(nominal, "A pauta exige corroboracao nominal de pessoas identificadas." if nominal else "A pauta nao exige busca nominal de individuos."),
            "second_fact_pass": _decision(nominal, "A segunda passagem so e necessaria quando ha nova coleta nominal." if nominal else "Sem coleta nominal, a segunda passagem factual nao e necessaria."),
            "classification": _decision(True, "Necessaria para enquadramento e analise da repercussao."),
            "report_writer": _decision(True, "Necessaria para produzir o relatorio final."),
            "qa": _decision(True, "Necessaria para auditoria final do relatorio."),
        },
        "fact_fields": list(profile.get("requested_fact_fields") or []),
        "rationale": "Plano heuristico conservador gerado sem decisao metodologica da LLM.",
    }


def _normalize_processes(raw: dict[str, Any] | None, fallback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = (raw or {}).get("processes") if isinstance(raw, dict) else None
    if not isinstance(source, dict):
        source = raw if isinstance(raw, dict) else {}

    normalized: dict[str, dict[str, Any]] = {}
    for process_name in _PROCESS_TO_FLAG:
        fallback_decision = (fallback.get("processes") or {}).get(process_name) or {}
        candidate = source.get(process_name) if isinstance(source, dict) else None
        if isinstance(candidate, dict) and isinstance(candidate.get("enabled"), bool):
            normalized[process_name] = {
                "enabled": bool(candidate["enabled"]),
                "reason": str(candidate.get("reason") or fallback_decision.get("reason") or "Decisao metodologica.")[:1000],
            }
        else:
            normalized[process_name] = {
                "enabled": bool(fallback_decision.get("enabled", False)),
                "reason": str(fallback_decision.get("reason") or "Fallback metodologico.")[:1000],
            }
    return normalized


def sanitize_execution_plan(project: Project, raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate planner output, enforce dependencies and user overrides."""
    fallback = heuristic_execution_plan(project)
    selected = (project.execution_profile or "AUTO").upper()

    if selected in EXECUTION_PROFILE_DEFAULTS:
        processes = deepcopy(fallback["processes"])
        preset = EXECUTION_PROFILE_DEFAULTS[selected]
        for process_name, flag_name in _PROCESS_TO_FLAG.items():
            enabled = bool(preset[flag_name])
            processes[process_name] = _decision(
                enabled,
                f"Definido pelo perfil explicito {selected}.",
            )
    else:
        processes = _normalize_processes(raw, fallback)

    # Core report steps are mandatory for this product.
    for process_name in ("web_collection", "media_validation", "classification", "report_writer", "qa"):
        processes[process_name]["enabled"] = True

    # Logical dependencies.
    if not processes["fact_extraction"]["enabled"]:
        processes["fact_resolution"] = _decision(False, "Sem extracao factual, nao ha fatos para consolidar.")
        processes["nominal_followup"] = _decision(False, "Busca nominal depende da camada factual.")
        processes["second_fact_pass"] = _decision(False, "Segunda passagem depende de nova coleta factual/nominal.")
    if not processes["nominal_followup"]["enabled"]:
        processes["second_fact_pass"] = _decision(False, "Sem busca nominal, nao ha segunda passagem factual.")

    # Explicit API overrides win over AUTO/planner decisions.
    overrides = project.execution_options or {}
    override_map = {
        "enable_youtube": "youtube_collection",
        "enable_fact_layer": "fact_extraction",
        "enable_nominal_followup": "nominal_followup",
        "enable_academic_research": "academic_research",
        "enable_social_repercussion": "social_repercussion",
    }
    for option_name, process_name in override_map.items():
        value = overrides.get(option_name)
        if isinstance(value, bool):
            processes[process_name] = _decision(value, f"Override explicito do usuario: {option_name}={value}.")

    # Re-apply dependencies after overrides.
    if not processes["fact_extraction"]["enabled"]:
        for name, reason in (
            ("fact_resolution", "Sem extracao factual, nao ha consolidacao."),
            ("nominal_followup", "Busca nominal depende da camada factual."),
            ("second_fact_pass", "Segunda passagem depende da camada factual."),
        ):
            processes[name] = _decision(False, reason)
    if not processes["nominal_followup"]["enabled"]:
        processes["second_fact_pass"] = _decision(False, "Sem busca nominal, nao ha segunda passagem factual.")

    fact_fields = []
    if isinstance(raw, dict):
        fact_fields = [str(value).strip() for value in (raw.get("fact_fields") or []) if str(value).strip()]
    if not fact_fields:
        fact_fields = list((project.topic_profile or {}).get("requested_fact_fields") or [])

    return {
        "version": 1,
        "mode": "EXPLICIT_PRESET" if selected in EXECUTION_PROFILE_DEFAULTS else "AUTO_PLANNED",
        "processes": processes,
        "fact_fields": list(dict.fromkeys(fact_fields))[:30],
        "rationale": str((raw or {}).get("rationale") or fallback.get("rationale") or "")[:3000],
    }


def effective_execution_profile(project: Project) -> str:
    selected = (project.execution_profile or "AUTO").upper()
    if selected in EXECUTION_PROFILE_DEFAULTS:
        return selected
    if project.execution_plan:
        return "AUTO_PLANNED"
    return "AUTO"


def execution_flags(project: Project) -> tuple[str, dict[str, bool]]:
    selected = (project.execution_profile or "AUTO").upper()
    if selected in EXECUTION_PROFILE_DEFAULTS:
        # sanitize also applies user overrides/dependencies for explicit presets.
        plan = sanitize_execution_plan(project, project.execution_plan or {})
    elif project.execution_plan:
        plan = sanitize_execution_plan(project, project.execution_plan)
    else:
        plan = sanitize_execution_plan(project, heuristic_execution_plan(project))

    processes = plan.get("processes") or {}
    flags = {
        flag_name: bool((processes.get(process_name) or {}).get("enabled"))
        for process_name, flag_name in _PROCESS_TO_FLAG.items()
    }
    return effective_execution_profile(project), flags

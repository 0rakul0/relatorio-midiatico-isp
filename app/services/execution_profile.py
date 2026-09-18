from __future__ import annotations

from app.models import Project


EXECUTION_PROFILE_DEFAULTS = {
    "MIDIATICO_SIMPLES": {
        "enable_youtube": True,
        "enable_fact_layer": False,
        "enable_nominal_followup": False,
        "enable_cross_validation": True,
    },
    "MIDIATICO_COM_FATOS": {
        "enable_youtube": True,
        "enable_fact_layer": True,
        "enable_nominal_followup": False,
        "enable_cross_validation": True,
    },
    "COMPLETO_NOMINAL": {
        "enable_youtube": True,
        "enable_fact_layer": True,
        "enable_nominal_followup": True,
        "enable_cross_validation": True,
    },
}

def effective_execution_profile(project: Project) -> str:
    """Resolve AUTO somente depois que o perfil temático já está disponível."""
    selected = (project.execution_profile or "AUTO").upper()
    if selected in EXECUTION_PROFILE_DEFAULTS:
        return selected

    if project.project_type == "INSTITUTIONAL_PRODUCT":
        return "MIDIATICO_SIMPLES"

    if project.project_type == "EVENT_TOPIC":
        profile = project.topic_profile or {}
        event_type = str(profile.get("event_type") or "").upper()
        requested_fields = set(profile.get("requested_fact_fields") or [])
        nominal_event_types = {"DEATH", "HOMICIDE", "FEMICIDE", "FEMINICIDE", "MURDER"}
        if "subject_name" in requested_fields and event_type in nominal_event_types:
            return "COMPLETO_NOMINAL"
        return "MIDIATICO_COM_FATOS"

    return "MIDIATICO_SIMPLES"


def execution_flags(project: Project) -> tuple[str, dict[str, bool]]:
    profile = effective_execution_profile(project)
    flags = dict(EXECUTION_PROFILE_DEFAULTS[profile])
    overrides = project.execution_options or {}
    for key in flags:
        value = overrides.get(key)
        if isinstance(value, bool):
            flags[key] = value

    # Dependências lógicas: não existe busca nominal sem camada factual,
    # nem validação cruzada Tavily x DuckDuckGo Videos sem coleta no YouTube.
    if not flags["enable_fact_layer"]:
        flags["enable_nominal_followup"] = False
    if not flags["enable_youtube"]:
        flags["enable_cross_validation"] = False
    return profile, flags



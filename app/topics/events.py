from __future__ import annotations

from app.topics.temporal import normalized_text


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


def is_state_intervention_death_topic(topic: str) -> bool:
    text = " ".join(normalized_text(topic).split())
    death_hit = any(
        term in text
        for term in ("morte", "mortes", "morto", "morta", "letalidade")
    )
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


def event_profile_from_topic(topic: str) -> dict:
    if is_state_intervention_death_topic(topic):
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

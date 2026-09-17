from datetime import date
from types import ModuleType, SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Project


def test_profile_discovery_never_exceeds_four_external_searches(monkeypatch):
    from app import services

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    project = Project(
        topic="Dossiê Mulher 2026",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 1, 31),
    )
    session.add(project)
    session.commit()

    calls = []

    class FakeTavilyClient:
        def __init__(self, api_key):
            assert api_key == "test-key"

        def search(self, *, query, **_kwargs):
            calls.append(query)
            return {
                "results": [
                    {"title": query, "url": f"https://example.test/{len(calls)}", "content": "evidência"}
                ]
            }

    fake_tavily = ModuleType("tavily")
    fake_tavily.TavilyClient = FakeTavilyClient
    monkeypatch.setitem(__import__("sys").modules, "tavily", fake_tavily)
    monkeypatch.setattr(
        services,
        "get_settings",
        lambda: SimpleNamespace(tavily_api_key="test-key", max_profile_discovery_calls=4),
    )
    monkeypatch.setattr(services, "llm_is_configured", lambda: True)
    # Este teste isola a cadeia Tavily; o DuckDuckGo é exercitado em outro lugar.
    monkeypatch.setattr(services, "duckduckgo_available", lambda: False)
    monkeypatch.setattr(
        services,
        "structured_response",
        lambda **_kwargs: {
            "institution": None,
            "product_status": "PUBLISHED",
            "product_evidence": "Produto publicado.",
            "product_source_index": 0,
            "launch_status": "NOT_FOUND",
            "launch_date": None,
            "expected_launch_date": None,
            "launch_evidence": None,
            "launch_source_index": None,
            "official_facts": [],
        },
    )
    monkeypatch.setattr(
        services,
        "build_topic_profile",
        lambda _topic: {"project_type": "INSTITUTIONAL_PRODUCT", "product_name": "Dossiê Mulher 2026"},
    )

    result = services.discover_project_profile(session, project)

    # O orçamento de 4 cobre TODAS as chamadas externas com LLM configurado:
    # 1 perfil do tema (build_topic_profile) + 1 análise documental final.
    # Sobram apenas 2 para as buscas. Sem a reserva do perfil, a etapa faria
    # 1 + 3 + 1 = 5 chamadas, excedendo max_profile_discovery_calls.
    assert len(calls) == 2
    assert result["discovery_stats"]["external_search_calls"] == 2
    assert result["discovery_stats"]["call_budget"] == 4
    assert result["discovery_stats"]["search_budget"] == 2
    assert result["discovery_stats"]["profile_analysis_calls"] == 1
    session.close()

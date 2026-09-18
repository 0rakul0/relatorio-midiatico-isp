from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.fact_layer import extract_project_facts, plan_nominal_followups, resolve_project_facts
from app.models import Project
from app.report_qa import run_report_qa
from app.services.execution_profile import execution_flags
from app.services.project_profile import project_payload, trusted_launch_date, discover_project_profile
from app.services.search_planning import plan_queries_with_llm
from app.services.collection.web import collect_web
from app.services.collection.youtube import collect_media_sources
from app.services.validation import validate_and_classify, validate_video_metadata_cross_source
from app.services.classification import classify_with_llm
from app.services.reporting import draft_report_with_llm


def run_full_methodology(
    db: Session,
    project: Project,
    *,
    progress_callback: Callable[[str, str, str | None], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """Run the required stages; every AI task goes through ReportAgent."""

    def check() -> None:
        if cancel_check:
            cancel_check()

    def stage(key: str, status: str, detail: str | None = None) -> None:
        if progress_callback:
            progress_callback(key, status, detail)

    def detail_for(key: str) -> Callable[[str], None]:
        return lambda detail: stage(key, "RUNNING", detail)

    settings = get_settings()

    # 1. Topic profile
    check()
    stage("profile", "RUNNING", "Classificando o tipo de pauta e estruturando o perfil do tema")
    if project.status in {"DRAFT", "CUSTOM_DATES", "PROFILE_NEEDS_REVIEW"} or not project.topic_profile:
        profile = discover_project_profile(db, project)
    else:
        profile = {
            "status": project.status,
            "project_type": project.project_type,
            "topic_profile": project.topic_profile,
        }

    profile_note = None
    if project.project_type == "INSTITUTIONAL_PRODUCT":
        profile_state = project.topic_profile or {}
        product_status = str(profile_state.get("product_status") or "NOT_CONFIRMED")
        launch_confirmed = bool(profile_state.get("launch_date_confirmed"))
        if product_status == "PUBLISHED":
            trusted_launch = trusted_launch_date(project)
            if launch_confirmed and trusted_launch:
                profile_note = f"produto publicado confirmado; lancamento real em {trusted_launch.isoformat()}"
            else:
                profile_note = "produto publicado confirmado; data exata de lancamento nao confirmada"
        elif product_status == "ANNOUNCED":
            expected = profile_state.get("expected_launch_date")
            profile_note = "produto anunciado; publicacao efetiva ainda nao confirmada"
            if expected:
                profile_note += f"; previsao localizada: {expected}"
        else:
            profile_note = "confirmacao documental do produto pendente; seguindo por busca tematica"

    execution_profile, flags = execution_flags(project)
    stage(
        "profile",
        "DONE",
        f"Tema: {project.project_type}; perfil de execucao: {execution_profile}"
        + (f"; {profile_note}" if profile_note else ""),
    )

    # 2. Search strategy. The LLM optimizes one primary query instead of
    # producing a large paraphrase list; portal checks are expanded by code.
    check()
    stage(
        "search_plan",
        "RUNNING",
        "Otimizando uma consulta principal e poucas complementares realmente distintas",
    )
    planned = plan_queries_with_llm(db, project)
    media_count = sum(1 for row in planned if row.purpose == "MEDIA_REPERCUSSION")
    fact_count = sum(1 for row in planned if row.purpose == "FACT_DISCOVERY")
    official_count = sum(1 for row in planned if row.purpose == "OFFICIAL_FACT")
    strategy = (project.topic_profile or {}).get("search_strategy") or {}
    complementary_count = len(strategy.get("complementary_queries") or [])
    stage(
        "search_plan",
        "DONE",
        f"Estrategia pronta: 1 consulta principal, {complementary_count} complementar(es), "
        f"{media_count} consulta(s) midiaticas totais incluindo portais, "
        f"{fact_count} factual(is) e {official_count} oficial(is)",
    )

    # 3. Web + optional YouTube collection
    check()
    stage(
        "collection",
        "RUNNING",
        f"Buscando corpus; meta midiática aproximada: {settings.target_media_items} URL(s) unicas",
    )
    if flags["enable_youtube"]:
        stage("youtube", "RUNNING", "Pesquisando videos: DuckDuckGo Videos -> Tavily")
    else:
        stage("youtube", "SKIPPED", f"Desativado pelo perfil {execution_profile}")

    collection_sources = collect_media_sources(
        db,
        project,
        enable_youtube=flags["enable_youtube"],
        cancel_check=check,
        web_progress=detail_for("collection"),
        youtube_progress=detail_for("youtube") if flags["enable_youtube"] else None,
    )
    web = collection_sources["web"]
    collected = int(web["collected"])
    web_stats = web.get("stats") or {}
    media_added = int(web_stats.get("media_added", 0))
    target = int(web_stats.get("target_media_items", settings.target_media_items))

    if web["status"] == "COMPLETED":
        provider_label = str(web.get("provider") or "duckduckgo")
        stage(
            "collection",
            "DONE",
            f"{collected} novo(s) item(ns) no total; {media_added}/{target} da meta midiática preliminar. "
            f"Provedores: {provider_label}. DuckDuckGo: {int(web_stats.get('duckduckgo_queries', 0))} consulta(s), "
            f"{int(web_stats.get('duckduckgo_added', 0))} novo(s); Tavily: "
            f"{int(web_stats.get('tavily_queries', 0))} consulta(s), {int(web_stats.get('tavily_added', 0))} novo(s).",
        )
    elif web["status"] == "PARTIAL":
        stage(
            "collection",
            "DONE",
            f"Coleta parcial: {collected} novo(s); {media_added}/{target} da meta midiática preliminar. "
            f"{int(web_stats.get('failed_queries', 0))} consulta(s) nao puderam ser concluidas.",
        )
    else:
        stage(
            "collection",
            "FAILED",
            f"Coleta web indisponivel nesta execucao: {str(web.get('error') or '')[:180]}",
        )

    youtube = collection_sources["youtube"]
    youtube_collected = int(youtube["collected"])
    project.youtube_collection_status = str(youtube["status"])
    project.youtube_collection_error = youtube.get("error")
    if project.youtube_collection_status == "DISABLED":
        stage("youtube", "SKIPPED", f"Desativado pelo perfil {execution_profile}")
    elif project.youtube_collection_status == "FALLBACK_DUCKDUCKGO":
        stage("youtube", "DONE", f"Fallback DuckDuckGo Videos concluido: {youtube_collected} video(s)")
    elif project.youtube_collection_status == "UNAVAILABLE":
        stage(
            "youtube",
            "SKIPPED",
            "Coleta no YouTube indisponivel; isso nao sera interpretado como ausencia de cobertura.",
        )
    elif project.youtube_collection_status == "NOT_CONFIGURED":
        stage("youtube", "SKIPPED", "Nenhum coletor do YouTube esta disponivel nesta execucao")
    else:
        stage("youtube", "DONE", f"{youtube_collected} video(s) coletado(s)")
    db.commit()

    # 4. Optional cross validation
    check()
    if not flags["enable_cross_validation"]:
        stage("cross_validation", "SKIPPED", "Opcional e desativada nesta execucao")
    else:
        stage("cross_validation", "RUNNING", "Comparando metadados coincidentes de video")
        try:
            cross_validation = validate_video_metadata_cross_source(
                db,
                project,
                cancel_check=check,
                progress_detail=detail_for("cross_validation"),
            )
        except RuntimeError as exc:
            stage("cross_validation", "SKIPPED", f"Validacao cruzada indisponivel: {str(exc)[:180]}")
        else:
            if cross_validation["skipped"]:
                if cross_validation.get("reason") == "NO_COMPARABLE_ITEMS":
                    stage(
                        "cross_validation",
                        "SKIPPED",
                        "Nao necessaria: nenhum video foi obtido por dois coletores independentes",
                    )
                else:
                    stage("cross_validation", "SKIPPED", "OPENAI_API_KEY nao configurada")
            else:
                stage("cross_validation", "DONE", f"{cross_validation['validated']} video(s) comparado(s)")

    fact_pass_1 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_1 = {"events": 0, "confirmed": 0, "partial": 0, "conflicts": 0}
    nominal_created = 0
    second_collected = 0
    fact_pass_2 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_2 = dict(fact_resolution_1)

    # 5. Optional fact layer
    fact_layer_active = flags["enable_fact_layer"] and project.project_type == "EVENT_TOPIC"
    if not fact_layer_active:
        reason = (
            f"Nao necessaria para o perfil {execution_profile}"
            if not flags["enable_fact_layer"]
            else f"Tipo de projeto {project.project_type} nao exige fatos individuais"
        )
        for key in (
            "facts_pass_1",
            "fact_resolution_1",
            "nominal_plan",
            "nominal_collection",
            "facts_pass_2",
            "fact_resolution_2",
        ):
            stage(key, "SKIPPED", reason)
    else:
        check()
        stage("facts_pass_1", "RUNNING", "Extraindo datas, causas, locais e evidencias por campo")
        fact_pass_1 = extract_project_facts(
            db,
            project,
            cancel_check=check,
            progress_detail=detail_for("facts_pass_1"),
        )
        stage("facts_pass_1", "DONE", f"{fact_pass_1['events_extracted']} evento(s) extraido(s)")

        check()
        stage("fact_resolution_1", "RUNNING", "Consolidando evidencias e detectando conflitos")
        fact_resolution_1 = resolve_project_facts(db, project)
        stage(
            "fact_resolution_1",
            "DONE",
            f"{fact_resolution_1['events']} evento(s); {fact_resolution_1['conflicts']} conflito(s)",
        )
        fact_resolution_2 = dict(fact_resolution_1)

        if not flags["enable_nominal_followup"]:
            stage("nominal_plan", "SKIPPED", f"Busca nominal desativada no perfil {execution_profile}")
            stage("nominal_collection", "SKIPPED", "Sem segunda coleta nominal")
            stage("facts_pass_2", "SKIPPED", "Sem segunda passagem factual")
            stage("fact_resolution_2", "SKIPPED", "A consolidacao da primeira passagem foi mantida")
        else:
            check()
            stage("nominal_plan", "RUNNING", "Criando buscas somente para nomes ja identificados")
            nominal_created = plan_nominal_followups(db, project)
            stage("nominal_plan", "DONE", f"{nominal_created} consulta(s) nominal(is) criada(s)")

            if nominal_created:
                check()
                stage("nominal_collection", "RUNNING", "Buscando corroboradores por nome")
                second_collected = collect_web(
                    db,
                    project.id,
                    cancel_check=check,
                    progress_detail=detail_for("nominal_collection"),
                )
                stage("nominal_collection", "DONE", f"{second_collected} novo(s) item(ns) coletado(s)")

                check()
                stage("facts_pass_2", "RUNNING", "Extraindo evidencias das novas fontes nominais")
                fact_pass_2 = extract_project_facts(
                    db,
                    project,
                    cancel_check=check,
                    progress_detail=detail_for("facts_pass_2"),
                )
                stage("facts_pass_2", "DONE", f"{fact_pass_2['events_extracted']} evento(s) adicional(is) extraido(s)")

                check()
                stage("fact_resolution_2", "RUNNING", "Reconciliando a camada factual")
                fact_resolution_2 = resolve_project_facts(db, project)
                stage(
                    "fact_resolution_2",
                    "DONE",
                    f"{fact_resolution_2['events']} evento(s); {fact_resolution_2['conflicts']} conflito(s)",
                )
            else:
                stage("nominal_collection", "SKIPPED", "Nenhum nome novo exigiu segunda coleta")
                stage("facts_pass_2", "SKIPPED", "Nenhuma segunda coleta para extrair")
                stage("fact_resolution_2", "SKIPPED", "A consolidacao da primeira passagem foi mantida")

    # 6. Media core
    check()
    stage("validation", "RUNNING", "Validando janela de publicacao e aderencia tematica")
    validation = validate_and_classify(
        db,
        project,
        cancel_check=check,
        progress_detail=detail_for("validation"),
    )
    stage(
        "validation",
        "DONE",
        f"{validation['valid']} valido(s); {validation['discarded']} descartado(s); "
        f"{validation.get('llm_calls', 0)} chamada(s) LLM em lote",
    )

    check()
    stage("classification", "RUNNING", "Classificando enquadramento, fidelidade e mencao institucional")
    classification = classify_with_llm(
        db,
        project,
        cancel_check=check,
        progress_detail=detail_for("classification"),
    )
    classification_detail = (
        f"{classification.get('updated', 0)} item(ns) classificado(s); "
        f"{classification.get('llm_calls', 0)} chamada(s) LLM em lote"
    )
    if classification.get("failed_batches"):
        classification_detail += f"; {classification['failed_batches']} lote(s) com falha"
    stage("classification", "DONE", classification_detail)

    check()
    report_detail = (
        "Redigindo relatorio com camada factual individual"
        if fact_layer_active
        else "Redigindo relatorio de repercussao sem camada factual individual"
    )
    stage("report", "RUNNING", report_detail)
    drafted = draft_report_with_llm(db, project)
    stage("report", "DONE", "Relatorio estruturado e persistido")

    check()
    stage("qa", "RUNNING", "Executando verificacoes deterministicas e auditoria final")
    qa = run_report_qa(db, project, drafted)
    stage("qa", "DONE", f"QA {qa['status']}")

    project.status = "REPORT_READY" if qa["approved"] else "REPORT_NEEDS_REVIEW"
    db.commit()

    return {
        "project": project_payload(project, for_report=True),
        "profile": profile,
        "execution_profile": execution_profile,
        "execution_flags": flags,
        "planned": len(planned),
        "collected": collected,
        "youtube_collected": youtube_collected,
        "nominal_queries_created": nominal_created,
        "second_pass_collected": second_collected,
        "fact_pass_1": fact_pass_1,
        "fact_resolution_1": fact_resolution_1,
        "fact_pass_2": fact_pass_2,
        "fact_resolution_2": fact_resolution_2,
        "validation": validation,
        "classification": classification,
        "qa": qa,
        **drafted,
    }

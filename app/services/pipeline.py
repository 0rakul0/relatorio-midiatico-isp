from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

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
    """Executa as etapas necessárias; toda IA passa pelo único ReportAgent."""

    def check() -> None:
        if cancel_check:
            cancel_check()

    def stage(key: str, status: str, detail: str | None = None) -> None:
        if progress_callback:
            progress_callback(key, status, detail)

    def detail_for(key: str) -> Callable[[str], None]:
        return lambda detail: stage(key, "RUNNING", detail)

    # 1. Perfil temático: necessário até no modo AUTO, pois decide o pipeline efetivo.
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
                profile_note = (
                    f"produto publicado confirmado · lançamento real confirmado em "
                    f"{trusted_launch.isoformat()}"
                )
            else:
                profile_note = "produto publicado confirmado · data exata de lançamento não confirmada"
        elif product_status == "ANNOUNCED":
            expected = profile_state.get("expected_launch_date")
            profile_note = "produto anunciado; publicação efetiva ainda não confirmada"
            if expected:
                profile_note += f" · previsão localizada: {expected}"
        else:
            profile_note = "confirmação documental do produto pendente; seguindo por busca temática"

    execution_profile, flags = execution_flags(project)
    stage(
        "profile",
        "DONE",
        (
            f"Tema: {project.project_type} · perfil de execução: {execution_profile}"
            + (f" · {profile_note}" if profile_note else "")
        ),
    )

    # 2. Planejamento de buscas. plan_queries_with_llm já respeita enable_fact_layer.
    check()
    stage("search_plan", "RUNNING", "Gerando consultas apenas para as finalidades habilitadas")
    planned = plan_queries_with_llm(db, project)
    stage("search_plan", "DONE", f"{len(planned)} consulta(s) criada(s)")

    # 3. Coleta web + YouTube opcional.
    check()
    stage("collection", "RUNNING", "Consultando fontes web: DuckDuckGo → Tavily")
    if flags["enable_youtube"]:
        stage("youtube", "RUNNING", "Pesquisando vídeos: DuckDuckGo Videos → Tavily")
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
    if web["status"] == "COMPLETED":
        provider_label = str(web.get("provider") or "duckduckgo")
        stage(
            "collection",
            "DONE",
            f"{collected} novo(s) item(ns). Provedores usados: {provider_label}. "
            f"DuckDuckGo: {int(web_stats.get('duckduckgo_queries', 0))} consulta(s), "
            f"{int(web_stats.get('duckduckgo_added', 0))} novo(s); "
            f"Tavily: {int(web_stats.get('tavily_queries', 0))} consulta(s), "
            f"{int(web_stats.get('tavily_added', 0))} novo(s).",
        )
    elif web["status"] == "PARTIAL":
        stage(
            "collection",
            "DONE",
            f"Coleta parcial: {collected} novo(s) item(ns). "
            f"DuckDuckGo {int(web_stats.get('duckduckgo_queries', 0))} consulta(s); "
            f"Tavily {int(web_stats.get('tavily_queries', 0))}; "
            f"{int(web_stats.get('failed_queries', 0))} consulta(s) não puderam ser concluídas.",
        )
    else:
        stage(
            "collection",
            "FAILED",
            f"Coleta web indisponível nesta execução: {str(web.get('error') or '')[:180]}",
        )

    youtube = collection_sources["youtube"]
    youtube_collected = int(youtube["collected"])
    project.youtube_collection_status = str(youtube["status"])
    project.youtube_collection_error = youtube.get("error")
    if project.youtube_collection_status == "DISABLED":
        stage("youtube", "SKIPPED", f"Desativado pelo perfil {execution_profile}")
    elif project.youtube_collection_status == "FALLBACK_DUCKDUCKGO":
        stage("youtube", "DONE", f"Fallback DuckDuckGo Videos concluído: {youtube_collected} vídeo(s)")
    elif project.youtube_collection_status == "UNAVAILABLE":
        stage(
            "youtube",
            "SKIPPED",
            "Coleta no YouTube indisponível; isso não será interpretado como ausência de cobertura.",
        )
    elif project.youtube_collection_status == "NOT_CONFIGURED":
        stage("youtube", "SKIPPED", "Nenhum coletor do YouTube está disponível nesta execução")
    else:
        stage("youtube", "DONE", f"{youtube_collected} vídeo(s) coletado(s)")
    db.commit()

    # 4. Validação cruzada só existe se YouTube estiver habilitado.
    check()
    if not flags["enable_cross_validation"]:
        stage("cross_validation", "SKIPPED", "Opcional e desativada nesta execução")
    else:
        stage("cross_validation", "RUNNING", "Comparando metadados coincidentes do Tavily e do DuckDuckGo Videos")
        try:
            cross_validation = validate_video_metadata_cross_source(
                db,
                project,
                cancel_check=check,
                progress_detail=detail_for("cross_validation"),
            )
        except RuntimeError as exc:
            stage("cross_validation", "SKIPPED", f"Validação cruzada indisponível: {str(exc)[:180]}")
        else:
            if cross_validation["skipped"]:
                if cross_validation.get("reason") == "NO_COMPARABLE_ITEMS":
                    stage(
                        "cross_validation",
                        "SKIPPED",
                        "Não necessária: nenhum vídeo foi obtido por dois coletores independentes",
                    )
                else:
                    stage("cross_validation", "SKIPPED", "OPENAI_API_KEY não configurada")
            else:
                stage("cross_validation", "DONE", f"{cross_validation['validated']} vídeo(s) comparado(s)")

    # Valores padrão permitem retornar um resultado estável mesmo quando a camada factual é pulada.
    fact_pass_1 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_1 = {"events": 0, "confirmed": 0, "partial": 0, "conflicts": 0}
    nominal_created = 0
    second_collected = 0
    fact_pass_2 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_2 = dict(fact_resolution_1)

    # 5. Camada factual opcional.
    fact_layer_active = flags["enable_fact_layer"] and project.project_type == "EVENT_TOPIC"
    if not fact_layer_active:
        reason = (
            f"Não necessária para o perfil {execution_profile}"
            if not flags["enable_fact_layer"]
            else f"Tipo de projeto {project.project_type} não exige fatos individuais"
        )
        for key in ("facts_pass_1", "fact_resolution_1", "nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(key, "SKIPPED", reason)
    else:
        check()
        stage("facts_pass_1", "RUNNING", "Extraindo datas, causas, locais e evidências por campo")
        fact_pass_1 = extract_project_facts(
            db,
            project,
            cancel_check=check,
            progress_detail=detail_for("facts_pass_1"),
        )
        stage("facts_pass_1", "DONE", f"{fact_pass_1['events_extracted']} evento(s) extraído(s)")

        check()
        stage("fact_resolution_1", "RUNNING", "Consolidando evidências e detectando conflitos entre fontes")
        fact_resolution_1 = resolve_project_facts(db, project)
        stage(
            "fact_resolution_1",
            "DONE",
            f"{fact_resolution_1['events']} evento(s); {fact_resolution_1['conflicts']} conflito(s)",
        )
        fact_resolution_2 = dict(fact_resolution_1)

        # Busca nominal é uma camada extra, reservada ao perfil completo ou override explícito.
        if not flags["enable_nominal_followup"]:
            stage("nominal_plan", "SKIPPED", f"Busca nominal desativada no perfil {execution_profile}")
            stage("nominal_collection", "SKIPPED", "Sem segunda coleta nominal")
            stage("facts_pass_2", "SKIPPED", "Sem segunda passagem factual")
            stage("fact_resolution_2", "SKIPPED", "A consolidação da primeira passagem foi mantida")
        else:
            check()
            stage("nominal_plan", "RUNNING", "Criando buscas somente para nomes já identificados")
            nominal_created = plan_nominal_followups(db, project)
            stage("nominal_plan", "DONE", f"{nominal_created} consulta(s) nominal(is) criada(s)")

            if nominal_created:
                check()
                stage("nominal_collection", "RUNNING", "Buscando corroboradores e fontes complementares por nome")
                second_collected = collect_web(
                    db,
                    project.id,
                    cancel_check=check,
                    progress_detail=detail_for("nominal_collection"),
                )
                stage("nominal_collection", "DONE", f"{second_collected} novo(s) item(ns) coletado(s)")

                check()
                stage("facts_pass_2", "RUNNING", "Extraindo evidências das novas fontes nominais")
                fact_pass_2 = extract_project_facts(
                    db,
                    project,
                    cancel_check=check,
                    progress_detail=detail_for("facts_pass_2"),
                )
                stage("facts_pass_2", "DONE", f"{fact_pass_2['events_extracted']} evento(s) adicional(is) extraído(s)")

                check()
                stage("fact_resolution_2", "RUNNING", "Reconciliando a camada factual com as novas fontes")
                fact_resolution_2 = resolve_project_facts(db, project)
                stage(
                    "fact_resolution_2",
                    "DONE",
                    f"{fact_resolution_2['events']} evento(s); {fact_resolution_2['conflicts']} conflito(s)",
                )
            else:
                stage("nominal_collection", "SKIPPED", "Nenhum nome novo exigiu segunda coleta")
                stage("facts_pass_2", "SKIPPED", "Nenhuma segunda coleta para extrair")
                stage("fact_resolution_2", "SKIPPED", "A consolidação da primeira passagem foi mantida")

    # 6. Núcleo midiático: sempre executado.
    check()
    stage("validation", "RUNNING", "Validando janela de publicação e aderência temática")
    validation = validate_and_classify(
        db,
        project,
        cancel_check=check,
        progress_detail=detail_for("validation"),
    )
    stage(
        "validation",
        "DONE",
        f"{validation['valid']} válido(s); {validation['discarded']} descartado(s); "
        f"{validation.get('llm_calls', 0)} chamada(s) LLM em lote",
    )

    check()
    stage("classification", "RUNNING", "Classificando enquadramento, fidelidade e menção institucional")
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
        "Redigindo relatório com camada factual individual"
        if fact_layer_active
        else "Redigindo relatório de repercussão sem camada factual individual"
    )
    stage("report", "RUNNING", report_detail)
    drafted = draft_report_with_llm(db, project)
    stage("report", "DONE", "Relatório estruturado e persistido")

    check()
    stage("qa", "RUNNING", "Executando verificações determinísticas e auditoria final")
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



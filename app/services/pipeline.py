from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.billing import plan_allows
from app.fact_layer import extract_project_facts, plan_nominal_followups, resolve_project_facts
from app.models import Project
from app.report_qa import run_report_qa
from app.services.article_hydration import hydrate_media_items
from app.services.classification import classify_with_llm
from app.services.collection.web import collect_web
from app.services.collection.youtube import collect_media_sources
from app.services.corpus_reuse import reuse_prior_corpus
from app.services.execution_profile import execution_flags
from app.services.news_validation import validate_news_stage
from app.services.project_profile import discover_project_profile, project_payload, trusted_launch_date
from app.services.reporting import draft_report_with_llm, refine_report_with_qa
from app.services.search_planning import plan_report_with_llm
from app.services.validation import validate_video_metadata_cross_source


def run_full_methodology(
    db: Session,
    project: Project,
    *,
    progress_callback: Callable[[str, str, str | None], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """Execute only the stages selected by the report plan."""

    def check() -> None:
        if cancel_check:
            cancel_check()

    def stage(key: str, status: str, detail: str | None = None) -> None:
        if progress_callback:
            progress_callback(key, status, detail)

    def detail_for(key: str) -> Callable[[str], None]:
        return lambda detail: stage(key, "RUNNING", detail)

    settings = get_settings()

    # 1. Topic profile -------------------------------------------------
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

    stage(
        "profile",
        "DONE",
        f"Tema classificado como {project.project_type}" + (f"; {profile_note}" if profile_note else ""),
    )

    # 2. Report planning + compact search strategy --------------------
    check()
    stage(
        "search_plan",
        "RUNNING",
        "Consultando o corpus historico e planejando apenas as lacunas da nova pauta",
    )
    reuse_summary = reuse_prior_corpus(
        db,
        project,
        progress_detail=detail_for("search_plan"),
    )
    planned = plan_report_with_llm(db, project)
    execution_profile, flags = execution_flags(project)

    media_count = sum(1 for row in planned if row.purpose == "MEDIA_REPERCUSSION")
    fact_count = sum(1 for row in planned if row.purpose == "FACT_DISCOVERY")
    official_count = sum(1 for row in planned if row.purpose == "OFFICIAL_FACT")
    strategy = (project.topic_profile or {}).get("search_strategy") or {}
    complementary_count = len(strategy.get("complementary_queries") or [])
    processes = (project.execution_plan or {}).get("processes") or {}
    enabled_optional = [
        label
        for key, label in (
            ("youtube_collection", "YouTube"),
            ("cross_validation", "validacao cruzada"),
            ("fact_extraction", "camada factual"),
            ("nominal_followup", "busca nominal"),
        )
        if bool((processes.get(key) or {}).get("enabled"))
    ]
    expired_docs = int(reuse_summary.get("expired_documents", 0) or 0)
    stage(
        "search_plan",
        "DONE",
        f"Plano {execution_profile}: {reuse_summary.get('reused', 0)} documento(s) historico(s) reutilizado(s) "
        f"de {reuse_summary.get('source_projects', 0)} projeto(s)"
        + (
            f"; {expired_docs} expirado(s) "
            f"(>{settings.corpus_reuse_max_age_days} dias) ignorado(s)"
            if expired_docs
            else ""
        )
        + f"; 1 consulta principal, "
        f"{complementary_count} complementar(es), {media_count} midiaticas, "
        f"{fact_count} factual(is), {official_count} oficial(is). "
        f"Opcionais ativos: {', '.join(enabled_optional) if enabled_optional else 'nenhum'}.",
    )

    # As soon as planning is done, expose optional stages that will not run.
    # This makes the execution tracker reflect the real methodology immediately.
    if not flags["enable_youtube"]:
        stage("youtube", "SKIPPED", (processes.get("youtube_collection") or {}).get("reason") or "Nao prevista no plano")
    if not flags["enable_cross_validation"]:
        stage("cross_validation", "SKIPPED", (processes.get("cross_validation") or {}).get("reason") or "Nao prevista no plano")
    if not flags["enable_fact_layer"]:
        fact_reason = (processes.get("fact_extraction") or {}).get("reason") or "Camada factual nao prevista no plano"
        for optional_key in ("facts_pass_1", "fact_resolution_1", "nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(optional_key, "SKIPPED", fact_reason)
    elif not flags["enable_nominal_followup"]:
        nominal_reason = (processes.get("nominal_followup") or {}).get("reason") or "Busca nominal nao prevista no plano"
        for optional_key in ("nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(optional_key, "SKIPPED", nominal_reason)

    # 3. Collection ----------------------------------------------------
    collected = youtube_collected = 0
    web_stats: dict = {}
    check()
    stage(
        "collection",
        "RUNNING",
        "Executando buscas aprovadas em modo leve: metadados/snippets e hits brutos auditaveis",
    )
    if flags["enable_youtube"]:
        stage("youtube", "RUNNING", "Pesquisando videos: DuckDuckGo Videos")
    else:
        reason = (processes.get("youtube_collection") or {}).get("reason") or "Desativado pelo plano"
        stage("youtube", "SKIPPED", reason)

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
    raw_hits = int(web_stats.get("raw_hits", 0))
    flagged_hits = int(web_stats.get("flagged_hits", 0))
    over_budget = int(web_stats.get("over_budget", 0))
    budget_cap = int(web_stats.get("new_item_budget", 0) or settings.max_new_media_items)

    if web["status"] in {"COMPLETED", "PARTIAL"}:
        provider_label = str(web.get("provider") or "duckduckgo")
        budget_note = (
            f" {over_budget} hit(s) fora do teto de {budget_cap} item(ns) novo(s)"
            " (preservados p/ auditoria, sem custo LLM)."
            if over_budget
            else f" Teto de itens novos: {budget_cap}."
        )
        stage(
            "collection",
            "DONE",
            f"{raw_hits} hit(s) bruto(s) preservado(s); {collected} URL(s) unica(s) nova(s); "
            f"{flagged_hits} hit(s) sinalizado(s). Provedores: {provider_label}." + budget_note + " "
            "O corpo completo das paginas sera obtido somente na validacao.",
        )
    else:
        stage("collection", "FAILED", f"Coleta web indisponivel: {str(web.get('error') or '')[:180]}")

    youtube = collection_sources["youtube"]
    youtube_collected = int(youtube["collected"])
    project.youtube_collection_status = str(youtube["status"])
    project.youtube_collection_error = youtube.get("error")
    if not flags["enable_youtube"] or project.youtube_collection_status == "DISABLED":
        stage("youtube", "SKIPPED", (processes.get("youtube_collection") or {}).get("reason") or "Desativado")
    elif project.youtube_collection_status == "UNAVAILABLE":
        stage("youtube", "SKIPPED", "Coleta de videos indisponivel; nao sera interpretada como ausencia de cobertura")
    else:
        stage("youtube", "DONE", f"{youtube_collected} video(s) consolidados")
    db.commit()

    # Cross-validation belongs to collection group (stage 3 in the UI).
    check()
    if not flags["enable_cross_validation"]:
        stage("cross_validation", "SKIPPED", (processes.get("cross_validation") or {}).get("reason") or "Nao necessaria")
    else:
        stage("cross_validation", "RUNNING", "Comparando metadados coincidentes de video")
        try:
            cross_validation = validate_video_metadata_cross_source(
                db, project, cancel_check=check, progress_detail=detail_for("cross_validation")
            )
        except RuntimeError as exc:
            stage("cross_validation", "SKIPPED", f"Validacao cruzada indisponivel: {str(exc)[:180]}")
        else:
            if cross_validation["skipped"]:
                stage("cross_validation", "SKIPPED", "Nenhum video comparavel por dois coletores independentes")
            else:
                stage("cross_validation", "DONE", f"{cross_validation['validated']} video(s) comparado(s)")

    # 4. News validation ------------------------------------------------
    validation = {
        "valid": 0,
        "discarded": 0,
        "llm_calls": 0,
        "hydration": {"requested": 0, "fetched": 0, "empty": 0, "errors": 0},
    }
    check()
    if not flags["enable_media_validation"]:
        stage("validation", "SKIPPED", (processes.get("media_validation") or {}).get("reason") or "Nao necessaria")
    else:
        stage(
            "validation",
            "RUNNING",
            "Deduplicando, hidratando URLs candidatas em paralelo e validando as noticias",
        )
        validation = validate_news_stage(
            db, project, cancel_check=check, progress_detail=detail_for("validation")
        )
        hydration = validation.get("hydration") or {}
        stage(
            "validation",
            "DONE",
            f"{validation.get('valid', 0)} noticia(s) valida(s) (meta: {settings.target_media_items}); "
            f"{validation.get('discarded', 0)} fora do corpus; "
            f"{hydration.get('fetched', 0)}/{hydration.get('requested', 0)} pagina(s) hidratada(s); "
            f"{validation.get('llm_calls', 0)} chamada(s) LLM em lote",
        )

    # Optional factual branch -----------------------------------------
    fact_pass_1 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_1 = {"events": 0, "confirmed": 0, "partial": 0, "conflicts": 0}
    nominal_created = 0
    second_collected = 0
    fact_pass_2 = {"processed": 0, "events_extracted": 0, "errors": 0}
    fact_resolution_2 = dict(fact_resolution_1)

    fact_layer_active = bool(flags["enable_fact_layer"])
    if not fact_layer_active:
        reason = (processes.get("fact_extraction") or {}).get("reason") or "Camada factual nao necessaria para esta pauta"
        for key in ("facts_pass_1", "fact_resolution_1", "nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(key, "SKIPPED", reason)
    else:
        check()
        stage("facts_pass_1", "RUNNING", "Extraindo fatos sustentados pelas fontes hidratadas")
        fact_pass_1 = extract_project_facts(db, project, cancel_check=check, progress_detail=detail_for("facts_pass_1"))
        stage("facts_pass_1", "DONE", f"{fact_pass_1['events_extracted']} evento(s) extraido(s)")

        if flags["enable_fact_resolution"]:
            check()
            stage("fact_resolution_1", "RUNNING", "Consolidando evidencias e detectando conflitos")
            fact_resolution_1 = resolve_project_facts(db, project)
            fact_resolution_2 = dict(fact_resolution_1)
            stage("fact_resolution_1", "DONE", f"{fact_resolution_1['events']} evento(s); {fact_resolution_1['conflicts']} conflito(s)")
        else:
            stage("fact_resolution_1", "SKIPPED", (processes.get("fact_resolution") or {}).get("reason") or "Nao necessaria")

        if not flags["enable_nominal_followup"]:
            reason = (processes.get("nominal_followup") or {}).get("reason") or "Busca nominal nao necessaria"
            stage("nominal_plan", "SKIPPED", reason)
            stage("nominal_collection", "SKIPPED", reason)
            stage("facts_pass_2", "SKIPPED", reason)
            stage("fact_resolution_2", "SKIPPED", reason)
        else:
            check()
            stage("nominal_plan", "RUNNING", "Criando buscas somente para nomes ja identificados")
            nominal_created = plan_nominal_followups(db, project)
            stage("nominal_plan", "DONE", f"{nominal_created} consulta(s) nominal(is) criada(s)")

            if nominal_created:
                check()
                stage("nominal_collection", "RUNNING", "Coletando corroboradores nominais em modo leve")
                second_collected = collect_web(
                    db, project.id, cancel_check=check, progress_detail=detail_for("nominal_collection")
                )
                hydration2 = hydrate_media_items(
                    db,
                    project,
                    purposes={"NOMINAL_FOLLOWUP"},
                    cancel_check=check,
                    progress_detail=detail_for("nominal_collection"),
                )
                stage(
                    "nominal_collection",
                    "DONE",
                    f"{second_collected} URL(s) nova(s); {hydration2.get('fetched', 0)} pagina(s) hidratada(s)",
                )

                if flags["enable_second_fact_pass"]:
                    check()
                    stage("facts_pass_2", "RUNNING", "Extraindo evidencias das novas fontes nominais")
                    fact_pass_2 = extract_project_facts(db, project, cancel_check=check, progress_detail=detail_for("facts_pass_2"))
                    stage("facts_pass_2", "DONE", f"{fact_pass_2['events_extracted']} evento(s) adicional(is) extraido(s)")

                    check()
                    stage("fact_resolution_2", "RUNNING", "Reconciliando a camada factual final")
                    fact_resolution_2 = resolve_project_facts(db, project)
                    stage("fact_resolution_2", "DONE", f"{fact_resolution_2['events']} evento(s); {fact_resolution_2['conflicts']} conflito(s)")
                else:
                    reason = (processes.get("second_fact_pass") or {}).get("reason") or "Segunda passagem nao necessaria"
                    stage("facts_pass_2", "SKIPPED", reason)
                    stage("fact_resolution_2", "SKIPPED", reason)
            else:
                stage("nominal_collection", "SKIPPED", "Nenhum nome identificado exigiu nova coleta")
                stage("facts_pass_2", "SKIPPED", "Sem nova coleta nominal")
                stage("fact_resolution_2", "SKIPPED", "Consolidacao anterior mantida")

    # 11. Classification ----------------------------------------------
    classification = {"updated": 0, "llm_calls": 0}
    check()
    if flags["enable_classification"]:
        stage("classification", "RUNNING", "Classificando enquadramento, fidelidade e mencao institucional")
        classification = classify_with_llm(db, project, cancel_check=check, progress_detail=detail_for("classification"))
        detail = f"{classification.get('updated', 0)} item(ns) classificado(s); {classification.get('llm_calls', 0)} chamada(s) LLM"
        if classification.get("failed_batches"):
            detail += f"; {classification['failed_batches']} lote(s) com falha"
        stage("classification", "DONE", detail)
    else:
        stage("classification", "SKIPPED", (processes.get("classification") or {}).get("reason") or "Nao necessaria")

    # 11b. Gap fill (fase 2) ------------------------------------------
    # Uma única rodada complementar por projeto: lacunas de cobertura
    # viram consultas abertas (web como um todo, sem site:).
    from app.services.search_planning import detect_coverage_gaps, plan_gap_fill_queries

    gap_fill: dict = {"created": 0, "collected": 0, "validated": 0, "status": "SKIPPED", "reason": ""}
    check()
    gap_state = (project.execution_plan or {}).get("gap_fill") or {}
    if gap_state.get("completed"):
        gap_fill["reason"] = "Cobertura complementar já executada neste projeto"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
    elif not flags.get("enable_web_collection", True):
        gap_fill["reason"] = "Coleta web desativada pelo plano"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
    elif not plan_allows(db, project, "gap_fill"):
        gap_fill["reason"] = "Plano atual não inclui cobertura complementar"
        stage("gap_fill", "SKIPPED", gap_fill["reason"])
    else:
        gaps = detect_coverage_gaps(db, project)
        if not gaps["needs_fill"]:
            gap_fill["reason"] = "Sem lacunas de portais prioritários"
            stage("gap_fill", "SKIPPED", gap_fill["reason"])
        else:
            portals = ", ".join(entry["portal"] for entry in gaps["uncovered_portals"])
            stage("gap_fill", "RUNNING", f"Lacunas em: {portals[:180]}")
            created = plan_gap_fill_queries(db, project, gaps)
            gap_fill["created"] = len(created)
            if not created:
                gap_fill["reason"] = "Nenhuma consulta complementar válida para as lacunas"
                stage("gap_fill", "SKIPPED", gap_fill["reason"])
            else:
                completed_ok = False
                try:
                    gap_fill["collected"] = collect_web(
                        db, project.id, cancel_check=check,
                        progress_detail=detail_for("gap_fill"),
                    )
                    gap_validation = validate_news_stage(
                        db, project, cancel_check=check,
                        progress_detail=detail_for("gap_fill"),
                    )
                    gap_fill["validated"] = int(gap_validation.get("valid", 0))
                    classify_with_llm(
                        db, project, cancel_check=check,
                        progress_detail=detail_for("gap_fill"),
                    )
                    completed_ok = True
                    stage(
                        "gap_fill", "DONE",
                        f"{len(created)} consulta(s) complementar(es); "
                        f"{gap_fill['collected']} URL(s) nova(s); "
                        f"{gap_fill['validated']} validada(s)",
                    )
                except RuntimeError as exc:
                    from app.orchestration.state import RunCancelled

                    if isinstance(exc, RunCancelled):
                        raise
                    # Best-effort: falha na coleta complementar não derruba o run
                    # e permite nova tentativa numa próxima execução.
                    gap_fill["reason"] = f"Coleta complementar indisponível: {str(exc)[:150]}"
                    stage("gap_fill", "DONE", gap_fill["reason"])
                if completed_ok:
                    plan_state = dict(project.execution_plan or {})
                    plan_state["gap_fill"] = {"completed": True, "created": gap_fill["created"]}
                    project.execution_plan = plan_state
                    db.commit()
                    gap_fill["status"] = "DONE"

    # 12. Report -------------------------------------------------------
    check()
    if not flags["enable_report_writer"]:
        raise RuntimeError("O plano desativou a redacao, mas esta etapa e obrigatoria para gerar o relatorio")
    stage("report", "RUNNING", "Redigindo o relatorio a partir do corpus validado e das camadas habilitadas")
    drafted = draft_report_with_llm(db, project)
    stage("report", "DONE", "Relatorio estruturado e persistido")

    # 13. QA + refinement loop -----------------------------------------
    check()
    if not flags["enable_qa"]:
        raise RuntimeError("O plano desativou QA, mas esta etapa e obrigatoria")
    stage("qa", "RUNNING", "Executando verificacoes deterministicas e auditoria final")
    qa = run_report_qa(db, project, drafted)
    refinements = 0
    if not qa["approved"]:
        drafted, qa, refinements = refine_report_with_qa(
            db, project, drafted, qa,
            progress_detail=detail_for("report"),
        )
    if refinements:
        stage("report", "DONE", f"Relatorio revisado {refinements}x a partir dos achados do QA")
    stage("qa", "DONE", f"QA {qa['status']}" + (f" apos {refinements} revisao(oes)" if refinements else ""))

    project.status = "REPORT_READY" if qa["approved"] else "REPORT_NEEDS_REVIEW"
    db.commit()

    return {
        "project": project_payload(project, for_report=True),
        "profile": profile,
        "execution_profile": execution_profile,
        "execution_plan": project.execution_plan,
        "execution_flags": flags,
        "corpus_reuse": reuse_summary,
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
        "gap_fill": gap_fill,
        "refinements": refinements,
        "qa": qa,
        **drafted,
    }

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.fact_layer import extract_project_facts, plan_nominal_followups, resolve_project_facts
from app.models import Project
from app.report_qa import run_report_qa
from app.services.article_hydration import hydrate_media_items
from app.services.academic_research import research_academic_literature
from app.services.classification import classify_with_llm
from app.services.collection.web import collect_web
from app.services.collection.youtube import collect_media_sources
from app.services.corpus_reuse import reuse_prior_corpus
from app.services.execution_profile import execution_flags
from app.services.news_validation import validate_news_stage
from app.services.project_profile import discover_project_profile, project_payload, trusted_launch_date
from app.services.reporting import draft_report_with_llm, refine_report_with_qa
from app.services.search_planning import plan_report_with_llm
from app.pipeline.optional_layers import run_public_opinion_layer, run_social_layer
from app.pipeline.gap_fill import run_gap_fill_stage


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
            ("social_repercussion", "percepcao nas redes sociais"),
            ("academic_research", "literatura cientifica"),
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
        f"{fact_count} factual(is), {official_count} oficial(is) "
        f"(incluindo varredura mensal em fontes primarias quando aplicavel). "
        f"Opcionais ativos: {', '.join(enabled_optional) if enabled_optional else 'nenhum'}.",
    )

    # As soon as planning is done, expose optional stages that will not run.
    # This makes the execution tracker reflect the real methodology immediately.
    if not flags["enable_academic_research"]:
        stage("academic_research", "SKIPPED", (processes.get("academic_research") or {}).get("reason") or "Nao prevista no plano")
    if not flags["enable_fact_layer"]:
        fact_reason = (processes.get("fact_extraction") or {}).get("reason") or "Camada factual nao prevista no plano"
        for optional_key in ("facts_pass_1", "fact_resolution_1", "nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(optional_key, "SKIPPED", fact_reason)
    elif not flags["enable_nominal_followup"]:
        nominal_reason = (processes.get("nominal_followup") or {}).get("reason") or "Busca nominal nao prevista no plano"
        for optional_key in ("nominal_plan", "nominal_collection", "facts_pass_2", "fact_resolution_2"):
            stage(optional_key, "SKIPPED", nominal_reason)

    # 3. Coleta paralela: corpus midiatico + literatura cientifica -----
    # Cada ramo usa uma SessionLocal propria. SQLAlchemy Session nao e thread-safe,
    # e manter as sessoes isoladas evita que commits/rollbacks de uma fonte
    # contaminem a outra.
    collected = youtube_collected = 0
    web_stats: dict = {}
    academic_research = {
        "searched": False,
        "queries": [],
        "summary": None,
        "candidates_returned": 0,
        "selected": 0,
        "persisted": 0,
        "papers": [],
    }

    def collect_media_branch() -> dict:
        branch_db = SessionLocal()
        try:
            branch_project = branch_db.get(Project, project.id)
            if not branch_project:
                raise RuntimeError("Projeto nao encontrado durante a coleta midiatica")
            return collect_media_sources(
                branch_db,
                branch_project,
                enable_youtube=True,
                cancel_check=check,
                web_progress=detail_for("collection"),
                youtube_progress=detail_for("youtube"),
            )
        finally:
            branch_db.close()

    def collect_academic_branch() -> dict:
        branch_db = SessionLocal()
        try:
            branch_project = branch_db.get(Project, project.id)
            if not branch_project:
                raise RuntimeError("Projeto nao encontrado durante a busca academica")
            return research_academic_literature(
                branch_db,
                branch_project,
                cancel_check=check,
                progress_detail=detail_for("academic_research"),
            )
        finally:
            branch_db.close()

    check()
    stage(
        "collection",
        "RUNNING",
        "Executando buscas aprovadas em modo leve: metadados/snippets e hits brutos auditaveis",
    )
    stage(
        "youtube",
        "RUNNING",
        "Classificando URLs do YouTube encontradas na descoberta principal do DuckDuckGo",
    )
    if flags["enable_academic_research"]:
        stage(
            "academic_research",
            "RUNNING",
            "Buscando literatura cientifica em paralelo com a coleta midiatica, sem mistura-la ao corpus de noticias",
        )

    academic_error: Exception | None = None
    workers = 2 if flags["enable_academic_research"] else 1
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report-sources") as pool:
        media_future = pool.submit(collect_media_branch)
        academic_future = (
            pool.submit(collect_academic_branch)
            if flags["enable_academic_research"]
            else None
        )

        collection_sources = media_future.result()
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
        stage(
            "youtube",
            "DONE",
            f"{youtube_collected} URL(s) do YouTube roteada(s) da descoberta DuckDuckGo",
        )
        db.commit()

        if academic_future is not None:
            try:
                academic_research = academic_future.result()
            except RuntimeError as exc:
                from app.orchestration.state import RunCancelled
                if isinstance(exc, RunCancelled):
                    raise
                academic_error = exc
            except SQLAlchemyError as exc:
                academic_error = exc

    if flags["enable_academic_research"]:
        if academic_error is not None:
            stage(
                "academic_research",
                "SKIPPED",
                f"Literatura cientifica indisponivel: {str(academic_error)[:180]}",
            )
        elif not academic_research.get("searched") and not academic_research.get("papers"):
            stage(
                "academic_research",
                "SKIPPED",
                academic_research.get("summary") or "O agente concluiu que a pauta nao exige contexto academico",
            )
        elif not academic_research.get("papers"):
            stage(
                "academic_research",
                "ERROR",
                f"Pesquisa academica executada, mas nenhum artigo foi selecionado. "
                f"Candidatos encontrados: {academic_research.get('candidates_returned', 0)}. "
                "Verifique consultas, resultados e criterios de relevancia.",
            )
        else:
            stage(
                "academic_research",
                "DONE",
                f"{academic_research.get('candidates_returned', 0)} candidato(s) academico(s); "
                f"{len(academic_research.get('papers') or [])} artigo(s) relevante(s) disponiveis no relatorio",
            )

    # 3b/3c. Camadas sociais e de opinião pública ----------------------
    social_repercussion = run_social_layer(
        db,
        project,
        stage=stage,
        check=check,
    )
    public_opinion = run_public_opinion_layer(
        db,
        project,
        stage=stage,
        check=check,
    )

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
    gap_fill = run_gap_fill_stage(
        db,
        project,
        flags=flags,
        stage=stage,
        check=check,
        detail_for=detail_for,
    )

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
        "academic_research": academic_research,
        "social_repercussion": social_repercussion,
        "public_opinion": public_opinion,
        "classification": classification,
        "gap_fill": gap_fill,
        "refinements": refinements,
        "qa": qa,
        **drafted,
    }

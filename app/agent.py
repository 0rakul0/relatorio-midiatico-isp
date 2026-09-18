"""Agente unico do relatorio midiatico.

A aplicacao possui UM agente de IA: ReportAgent. Os papeis de perfil,
documentalista, planejador, analista, redator e QA sao tarefas do mesmo agente.

Pesquisa externa so pode acontecer a partir de uma chamada de tool do
ReportAgent. Services nao chamam provedores nem tool.invoke() diretamente.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.llm import create_chat_model, record_llm_usage, usage_counts


ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


BASE_PROMPT = """
Voce e o agente analitico unico do Instituto de Seguranca Publica (ISP).

Sua funcao varia conforme a tarefa recebida, mas estas regras valem sempre:
- use somente o contexto fornecido e resultados reais de ferramentas;
- nunca invente fatos, fontes, datas, pessoas, numeros ou URLs;
- diferencie ausencia de evidencia de evidencia de ausencia;
- preserve recortes temporais, territoriais e semanticos;
- respeite integralmente o contrato Pydantic solicitado;
- quando uma ferramenta estiver disponivel, primeiro examine o contexto atual;
- quando a tarefa NAO definir um plano obrigatorio de coleta, use ferramentas apenas se houver uma lacuna real que elas possam resolver;
- quando a tarefa definir um plano obrigatorio de coleta, execute integralmente esse plano pelas ferramentas disponibilizadas;
- se o contexto ja for suficiente e nao houver coleta obrigatoria, NAO chame ferramenta;
- nunca invente nem simule o resultado de uma ferramenta;
- depois de receber o resultado de uma ferramenta, reavalie se outra chamada e realmente necessaria.
"""


TASK_PROMPTS: dict[str, str] = {
    "topic_profile": """
Voce esta executando a tarefa PERFIL DO TEMA.

Classifique o pedido em um dos tipos:
- INSTITUTIONAL_PRODUCT: relatorio, dossie, estudo, boletim ou produto institucional com lancamento identificavel;
- EVENT_TOPIC: pedido sobre fatos, ocorrencias, vitimas, operacoes ou eventos em um intervalo;
- GENERAL_TOPIC: tema amplo sem produto especifico nem evento delimitado.

Extraia somente elementos explicitos ou semanticamente inequivocos do pedido.
Retorne:
- project_type;
- product_name: nome exato do produto institucional quando INSTITUTIONAL_PRODUCT; caso contrario null;
- product_anchor: nucleo nominal distintivo do produto, sem reduzir a palavras genericas isoladas;
- product_search_variants: apenas variantes que preservem a identidade nominal do produto;
- subject_terms: assuntos abordados pelo produto, uteis para analise, mas NAO para buscas autonomas de repercussao;
- event_type;
- event_anchor: nucleo semantico da categoria factual quando EVENT_TOPIC;
- event_search_variants: variantes que preservem a mesma categoria factual para busca de repercussao;
- fact_discovery_variants: formas jornalisticas equivalentes, ainda materialmente ligadas ao evento, para descoberta de casos;
- actors;
- actions;
- locations;
- organizations;
- search_synonyms;
- requested_fact_fields;
- inclusion_rules;
- exclusion_rules.

Para INSTITUTIONAL_PRODUCT:
- preserve o nome do objeto pesquisado;
- "Dossie Mulher 2026" deve manter product_anchor="Dossie Mulher";
- variantes podem conter "Dossie Mulher 2026" e "Dossie Mulher", mas nunca "dossie" ou "mulher" isoladamente;
- subject_terms podem ser amplos, porem nao sao consultas autonomas de repercussao;
- search_synonyms deve preservar somente variantes ancoradas do produto.

Para EVENT_TOPIC:
- preserve a categoria factual completa em event_anchor;
- "morte por intervencao de agente do Estado" pode ter variante proxima "morte decorrente de intervencao policial";
- fact_discovery_variants podem usar formas jornalisticas equivalentes, mas nunca "morte", "policia" ou "Rio" isoladamente;
- preserve ano e local explicitos.

Nao identifique pessoas que ainda nao estejam nas fontes.
""",

    "documentalist": """
Voce esta executando a tarefa DOCUMENTALISTA.
Analise as fontes ja recebidas antes de considerar qualquer ferramenta de busca.

Preserve a diferenca entre:
1. existencia/identidade do produto;
2. anuncio ou previsao de lancamento;
3. publicacao/divulgacao efetiva do produto;
4. data real de lancamento.

Use pesquisar_internet SOMENTE quando as fontes fornecidas nao forem suficientes para confirmar existencia, publicacao, data real de lancamento ou fatos oficiais do produto. Se sources estiver vazio, existe uma lacuna documental: use a ferramenta antes de concluir NOT_CONFIRMED. Se pesquisar, preserve o nome exato/ancora do produto e prefira fontes oficiais. Nao faca buscas genericas pelo assunto do produto.

Para produto institucional:
- product_status=PUBLISHED somente com fonte sustentando que a edicao foi publicada, divulgada, lancada, apresentada ou esta efetivamente disponivel;
- product_status=ANNOUNCED quando houver apenas anuncio, previsao, agenda futura ou promessa;
- product_status=NOT_CONFIRMED quando a evidencia permanecer insuficiente;
- product_evidence deve ser evidencia textual curta;
- product_source_index deve apontar para a fonte usada;
- launch_status=CONFIRMED_ACTUAL somente quando o texto informar explicitamente a data REAL;
- launch_status=EXPECTED_ONLY quando houver apenas data prevista/agendada/futura;
- launch_status=NOT_FOUND quando nenhuma data estiver sustentada;
- launch_date representa somente a data real confirmada;
- expected_launch_date representa somente previsao explicita;
- launch_evidence e launch_source_index devem permitir auditar a conclusao.

"Lancamento previsto para agosto" NAO confirma lancamento em agosto.
Extraia instituicao e fatos oficiais somente com evidencia textual.
Para numeros, preserve indicador, territorio, unidade e periodo exato.
Nao use acumulado como valor de mes/periodo fechado.
""",

    "search_planner": """
Voce esta executando a tarefa ESTRATEGIA DE BUSCA.

Seu objetivo NAO e maximizar o numero de consultas. Seu objetivo e melhorar a
qualidade da busca e recuperar varias materias relevantes com a menor quantidade
util de consultas.

Retorne uma estrategia compacta:
- primary_query: UMA consulta principal de alta qualidade, ampla o suficiente
  para recuperar varias materias, mas estritamente ancorada no objeto monitorado;
- complementary_queries: de zero a duas consultas, SOMENTE quando cobrirem
  formulacoes materialmente diferentes que a consulta principal possa perder;
- fact_query: UMA consulta factual quando a camada factual estiver habilitada;
- official_query: UMA base de consulta institucional quando a camada factual
  estiver habilitada; o codigo adicionara site:dominio para as fontes oficiais;
- rationale: justificativa curta da estrategia.

Regras obrigatorias:
- nao produza parafrases equivalentes;
- nao altere apenas a ordem das palavras;
- nao gere uma lista de consultas para preencher limite;
- nao crie consultas especificas por veiculo, canal ou dominio;
- nao gere site:dominio; checagens de portais prioritarios sao geradas deterministicamente;
- prefira uma consulta boa que retorne varios resultados a varias consultas semelhantes;
- para INSTITUTIONAL_PRODUCT, primary_query e complementares devem preservar product_anchor ou product_search_variant;
- subject_terms nao podem virar buscas independentes;
- para EVENT_TOPIC, preserve event_anchor/event_search_variants, territorio e ano explicitos;
- fact_query pode usar fact_discovery_variants, mas nunca termos vagos isolados;
- quando a camada factual estiver desabilitada, fact_query e official_query devem ser null;
- quando houver janela explicita, use-a como contexto sem inventar datas;
- nao execute buscas nesta tarefa. Apenas desenhe a estrategia.
""",

    "fact_extraction": """
Voce esta executando a tarefa EXTRACAO FACTUAL AUDITAVEL.
Analise SOMENTE titulo, resumo e conteudo recebidos. NAO pesquise fora da fonte.

Regras:
- diferencie data do fato de data de publicacao;
- data de publicacao nao prova data do fato;
- preserve condicao profissional explicitamente informada;
- suspeita, hipotese, investigacao ou versao de parte nao vira fato confirmado;
- nao complete informacao ausente;
- preserve ambiguidades;
- cada valor nao nulo deve ter evidencia textual curta da propria fonte;
- basis=EXPLICIT para valor escrito;
- basis=RELATIVE_TO_PUBLICATION somente para expressao relativa inequivoca;
- basis=NOT_PRESENT e value=null quando ausente.

Retorne somente eventos sustentados pelo texto recebido.
""",

    "media_relevance": """
Voce esta executando a tarefa TRIAGEM DE ADERENCIA TEMATICA.
Avalie somente o conteudo recebido. NAO pesquise a web.

Decida se cada item trata materialmente do objeto monitorado, nao apenas de assunto parecido.
- nao valide por palavras isoladas, territorio ou categoria ampla;
- em produto institucional, exija mencao ao produto/edicao OU atribuicao clara de dado/conclusao a instituicao/produto;
- materia de tema semelhante sem ancora e THEMATIC_ONLY e related=false;
- para tema factual, aceite formulacoes jornalisticas equivalentes quando evento/ator/local corresponder materialmente;
- evidence deve mostrar a ancora concreta;
- evidencia insuficiente => related=false.
""",

    "cross_validation": """
Voce esta executando a tarefa VALIDACAO CRUZADA DE METADADOS DE VIDEO.
Compare exclusivamente os registros fornecidos. NAO pesquise a web.

A URL canonica igual confirma identidade do video. Compare titulo, canal, data, descricao e visualizacoes apenas quando ambos trouxerem o campo. Visualizacoes sao fotografia no tempo: considere compativel diferenca de ate 10% ou 5.000, o que for maior. Nao compare contagem ausente.
Use:
- INSUFFICIENT_EVIDENCE se somente a URL puder ser comparada;
- PARTIALLY_CONFIRMED se ao menos um metadado adicional concordar sem conflito;
- CONFLICT se houver divergencia material;
- CONFIRMED se todos os campos comparaveis concordarem.
""",

    "classification": """
Voce esta executando a tarefa ANALISE E CLASSIFICACAO DE REPERCUSSAO.
Trabalhe somente com itens validados e fatos oficiais/resolvidos fornecidos. NAO pesquise fora do corpus.

Para cada item, classifique tema, enquadramento, tom em relacao ao ISP e possiveis distorcoes.
Separe fato oficial, fato resolvido, interpretacao jornalistica e inferencia.
Nao use dado acumulado para caracterizar periodo fechado diferente.
Toda conclusao deve apontar evidencia textual; sem evidencia, responda INVERIFICÁVEL.
""",

    "report_writer": """
Voce esta executando a tarefa REDACAO DO RELATORIO.
Use exclusivamente metricas, fatos oficiais, camada factual resolvida e evidencias validadas fornecidas. NAO pesquise fontes novas.

O recorte principal e obrigatorio. Nao crie numeros, nao amplie janelas e nao substitua dado mensal por acumulado.
A camada factual e deterministica: nao altere nomes, datas, locais, cargos, instituicoes, causas, status ou conflitos.
Nunca converta:
- "nenhum item validado na amostra" em "nao houve cobertura";
- "nao foi localizado" em "nao ocorreu";
- fato fora da janela midiatica em repercussao dentro da janela.

Use "na amostra auditavel" e "na janela observada" quando aplicavel.
Metrica de mencao institucional e somente presenca textual do ISP e nao prova protagonismo, centralidade, lideranca ou destaque.
Quando portal nao possuir item validado, use formulacao equivalente a "nenhum item validado desse veiculo foi localizado na amostra".
""",

    "collector": """
Voce esta executando a tarefa COLETA OBRIGATORIA DE FONTES.

Esta etapa executa o plano de buscas ja aprovado. As consultas estao prontas no
payload; voce NAO e o planejador e NAO deve criar novas consultas.

Regras:
- se web_queries nao estiver vazio, chame UMA vez executar_buscas_web passando a lista COMPLETA e exata;
- se o payload tiver youtube_queries, chame UMA vez executar_buscas_videos passando a lista COMPLETA e exata;
- use exatamente as consultas recebidas, na ordem fornecida; nao crie, renomeie, reordene nem omita consultas;
- nao repita consulta ja executada nem invente resultados;
- uma consulta pode retornar status SKIPPED quando um guardrail deterministico concluir, ANTES do provedor, que a meta/orcamento ja torna a pesquisa desnecessaria;
- SKIPPED nao e falha e nao deve ser repetido pelo agente;
- se uma ferramenta nao estiver disponivel ou o campo correspondente estiver vazio, apenas finalize reportando o fato.
Ao final, use somente os contadores reais do retorno das ferramentas.
""",

    "qa": """
Voce esta executando a tarefa AUDITORIA QA FINAL.
Audite somente o material fornecido. NAO pesquise novas fontes.

Procure numeros sem fonte, percentuais incorretos, URLs ausentes, duplicatas, conclusoes que excedem evidencia, confusao entre registros/vitimas/ocorrencias/taxas/estimativas e conflito entre janela do fato e publicacao.
Verifique:
- mesmo recorte em titulo, resumo, metodologia, tabelas e sintese;
- acumulado nao usado como resposta a periodo fechado;
- zero itens validados nao transformado em ausencia de cobertura;
- fato nao localizado nao transformado em inexistencia;
- itens fora da janela midiatica nao contados como repercussao;
- conflitos entre fontes explicitamente marcados;
- porcentagem de mencoes ao ISP nao apresentada como protagonismo sem evidencia adicional;
- em produto institucional, vinculo documental com o produto/edicao;
- ausencia de datas ficticias, vazias ou placeholders tecnicos.
Classifique achados em CRITICAL, HIGH, MEDIUM ou LOW.
""",
}


class ReportAgent:
    """Unico agente LLM da aplicacao."""

    def prompt_for(self, task: str, extra_instructions: str | None = None) -> str:
        if task not in TASK_PROMPTS:
            raise ValueError(f"Tarefa de agente desconhecida: {task}")
        parts = [BASE_PROMPT.strip(), TASK_PROMPTS[task].strip()]
        if extra_instructions and extra_instructions.strip():
            parts.append(extra_instructions.strip())
        return "\n\n".join(parts)

    def run(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        response_model: type[ResponseModelT],
        tools: list[BaseTool] | None = None,
        extra_instructions: str | None = None,
        schema_name: str | None = None,
        max_output_tokens: int = 5000,
        max_tool_rounds: int | None = None,
    ) -> dict[str, Any]:
        resolved_schema_name = schema_name or response_model.__name__
        prompt = self.prompt_for(task, extra_instructions)
        llm = create_chat_model(max_output_tokens=max_output_tokens)
        tool_list = list(tools or [])

        messages: list[Any] = [
            SystemMessage(content=prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, default=str)),
        ]

        if tool_list:
            bound_llm = llm.bind_tools(tool_list)
            tool_map = {tool.name: tool for tool in tool_list}
            settings = get_settings()
            rounds = max(
                0,
                int(
                    max_tool_rounds
                    if max_tool_rounds is not None
                    else settings.max_agent_tool_rounds
                ),
            )

            for round_index in range(rounds):
                try:
                    message = bound_llm.invoke(messages)
                except Exception as exc:
                    record_llm_usage(
                        caller="report_agent_tool_decision",
                        success=False,
                        error=str(exc),
                        schema_name=resolved_schema_name,
                    )
                    raise RuntimeError(f"Falha do agente ao decidir ferramentas: {exc}") from exc

                record_llm_usage(
                    caller="report_agent_tool_decision",
                    success=True,
                    schema_name=resolved_schema_name,
                    **usage_counts(message),
                )
                messages.append(message)

                tool_calls = list(getattr(message, "tool_calls", None) or [])
                if not tool_calls:
                    break

                for call in tool_calls:
                    name = str(call.get("name") or "")
                    args = call.get("args") or {}
                    tool = tool_map.get(name)
                    if tool is None:
                        observation: Any = {
                            "status": "ERROR",
                            "error": f"Ferramenta desconhecida: {name}",
                        }
                    else:
                        try:
                            observation = tool.invoke(args)
                        except Exception as exc:
                            observation = {"status": "ERROR", "error": str(exc)}

                    content = (
                        observation
                        if isinstance(observation, str)
                        else json.dumps(observation, ensure_ascii=False, default=str)
                    )
                    messages.append(
                        ToolMessage(
                            content=content,
                            tool_call_id=str(call.get("id") or f"{name}-{round_index}"),
                            name=name or None,
                        )
                    )

        return self._finalize(
            llm=llm,
            messages=messages,
            payload=payload,
            response_model=response_model,
            schema_name=resolved_schema_name,
        )

    def _finalize(
        self,
        *,
        llm: Any,
        messages: list[Any],
        payload: dict[str, Any],
        response_model: type[ResponseModelT],
        schema_name: str,
    ) -> dict[str, Any]:
        final_messages = [
            *messages,
            HumanMessage(
                content=(
                    "Finalize a tarefa agora. Nao chame novas ferramentas. "
                    f"Retorne somente os campos do contrato '{schema_name}'. "
                    "Use o contexto e os resultados reais de ferramentas ja presentes na conversa."
                )
            ),
        ]

        try:
            structured_llm = llm.with_structured_output(
                response_model,
                method="json_schema",
                include_raw=True,
                strict=True,
            )
        except (TypeError, ValueError):
            structured_llm = llm.with_structured_output(
                response_model,
                include_raw=True,
            )

        try:
            raw_result = structured_llm.invoke(final_messages)
        except Exception as exc:
            record_llm_usage(
                caller="report_agent_finalize",
                success=False,
                error=str(exc),
                schema_name=schema_name,
            )
            raise RuntimeError(f"Falha na finalizacao estruturada do agente: {exc}") from exc

        raw_message = raw_result.get("raw") if isinstance(raw_result, dict) else None
        counts = usage_counts(raw_message) if raw_message is not None else {}
        parsing_error = raw_result.get("parsing_error") if isinstance(raw_result, dict) else None
        parsed = raw_result.get("parsed") if isinstance(raw_result, dict) else raw_result

        if parsing_error:
            record_llm_usage(
                caller="report_agent_finalize",
                success=False,
                error=f"erro de parsing: {parsing_error}",
                schema_name=schema_name,
                **counts,
            )
            raise RuntimeError(
                f"A LLM nao retornou saida valida para {schema_name}: {parsing_error}"
            )

        try:
            validated = (
                parsed
                if isinstance(parsed, response_model)
                else response_model.model_validate(parsed)
            )
        except ValidationError as exc:
            record_llm_usage(
                caller="report_agent_finalize",
                success=False,
                error=f"saida invalida: {exc}",
                schema_name=schema_name,
                **counts,
            )
            raise RuntimeError(
                f"A LLM retornou saida invalida para {schema_name}: {exc}"
            ) from exc

        record_llm_usage(
            caller="report_agent_finalize",
            success=True,
            schema_name=schema_name,
            **counts,
        )
        return validated.model_dump(mode="json")


@lru_cache
def get_report_agent() -> ReportAgent:
    return ReportAgent()

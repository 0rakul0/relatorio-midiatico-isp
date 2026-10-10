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
from app.prompts.report_agent import BASE_PROMPT, TASK_PROMPTS

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


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
        tool_list = list(tools or [])

        # O collector nao decide estrategia: ele recebe um plano fechado e
        # aprovado pelos estagios anteriores. Executar esse plano via LLM cria
        # uma dependencia desnecessaria (e fragil) de tool-calling. A execucao
        # continua dentro do ReportAgent, portanto a arquitetura permanece:
        # services -> agent -> tools -> providers.
        if task == "collector":
            return self._run_collector(
                payload=payload,
                response_model=response_model,
                tools=tool_list,
            )

        if task == "academic_research" and payload.get("require_academic_search"):
            tool = next((item for item in tool_list if item.name == "pesquisar_literatura_cientifica"), None)
            if tool is None:
                raise RuntimeError("A ferramenta de literatura cientifica obrigatoria nao esta disponivel.")
            topic = str((payload.get("project") or {}).get("topic") or "").strip()
            terms = [topic] if topic else []
            normalized = topic.casefold()
            if "mulher" in normalized and "viol" in normalized:
                terms.extend(["violencia de genero feminicidio Brasil", "violence against women Brazil"])
            elif topic:
                terms.append(topic + " Brasil pesquisa cientifica")
            terms = list(dict.fromkeys(terms))[:3]
            observation = tool.invoke({"queries": terms, "max_results_per_query": 8})
            payload = dict(payload)
            payload["mandatory_academic_search"] = observation
            payload["mandatory_academic_queries"] = terms

        llm = create_chat_model(max_output_tokens=max_output_tokens)
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

    def _run_collector(
        self,
        *,
        payload: dict[str, Any],
        response_model: type[ResponseModelT],
        tools: list[BaseTool],
    ) -> dict[str, Any]:
        """Executa deterministicamente o plano de coleta ja aprovado."""
        tool_map = {tool.name: tool for tool in tools}
        required: list[tuple[str, list[str]]] = []

        web_queries = list(payload.get("web_queries") or [])
        if web_queries:
            required.append(("executar_buscas_web", web_queries))

        if not required:
            result = response_model(
                status="COMPLETED",
                detail="Nenhuma consulta pendente no plano aprovado.",
            )
            return result.model_dump(mode="json")

        summaries: list[str] = []
        partial = False
        for tool_name, queries in required:
            tool = tool_map.get(tool_name)
            if tool is None:
                raise RuntimeError(
                    f"Ferramenta obrigatoria '{tool_name}' nao esta disponivel "
                    "para executar o plano aprovado"
                )

            try:
                observation = tool.invoke({"queries": queries})
            except Exception as exc:
                raise RuntimeError(
                    f"Falha ao executar a ferramenta obrigatoria '{tool_name}': {exc}"
                ) from exc

            result_rows = []
            if isinstance(observation, dict):
                result_rows = list(observation.get("results") or [])
            errors = [
                row for row in result_rows
                if str(row.get("status") or "").upper() in {"ERROR", "SKIPPED"}
            ]
            if errors:
                partial = True

            summaries.append(
                f"{tool_name}: {len(queries)} consulta(s) executada(s)"
                + (f", {len(errors)} com erro/skip" if errors else "")
            )

        result = response_model(
            status="PARTIAL" if partial else "COMPLETED",
            detail="; ".join(summaries),
        )
        return result.model_dump(mode="json")

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

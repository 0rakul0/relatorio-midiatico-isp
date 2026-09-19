"""Monitor de custo e consumo de tokens das chamadas OpenAI.

O registro é feito por invocação observada pelo ReportAgent, com custo
estimado em USD a partir de uma tabela de preços local. Retries internos do
SDK não são necessariamente observáveis como linhas independentes.

O contexto (projeto/operação/run) é propagado via ``contextvars`` para não
obrigar cada chamador a repassar esses dados; ``llm.py`` apenas registra o
que a API devolveu.
"""

import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import LLMCall

logger = logging.getLogger(__name__)

# Preços por 1M de tokens em USD, conforme tabela oficial da OpenAI
# (últimos valores conferidos em 2026-09). Modelos sem entrada explícita
# usam o fallback configurável (_FALLBACK_MODEL_PRICE).
MODEL_PRICES: dict[str, dict[str, Decimal]] = {
    "gpt-5-nano": {"input": Decimal("0.05"), "output": Decimal("0.40")},
    "gpt-5-mini": {"input": Decimal("0.25"), "output": Decimal("2.00")},
    "gpt-5": {"input": Decimal("1.25"), "output": Decimal("10.00")},
    "gpt-5-pro": {"input": Decimal("15.00"), "output": Decimal("120.00")},
    "gpt-5.1": {"input": Decimal("1.25"), "output": Decimal("10.00")},
    "gpt-5.2": {"input": Decimal("1.75"), "output": Decimal("14.00")},
    "gpt-5.2-pro": {"input": Decimal("21.00"), "output": Decimal("168.00")},
    "gpt-5.4-mini": {"input": Decimal("0.75"), "output": Decimal("4.50")},
    "gpt-5.4-nano": {"input": Decimal("0.20"), "output": Decimal("1.25")},
    "gpt-5.4": {"input": Decimal("2.50"), "output": Decimal("15.00")},
    "gpt-5.5": {"input": Decimal("5.00"), "output": Decimal("30.00")},
    "gpt-5.5-pro": {"input": Decimal("30.00"), "output": Decimal("180.00")},
    "gpt-4.1-nano": {"input": Decimal("0.10"), "output": Decimal("0.40")},
    "gpt-4.1-mini": {"input": Decimal("0.40"), "output": Decimal("1.60")},
    "gpt-4.1": {"input": Decimal("2.00"), "output": Decimal("8.00")},
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
    "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
}

# Modelo genérico usado quando o nome exato não está na tabela.
_FALLBACK_MODEL_PRICE = {
    "input": Decimal("0.15"),
    "output": Decimal("0.60"),
}

# Campo legado. A arquitetura atual não usa OpenAI Web Search; as pesquisas
# externas são DuckDuckGo e não entram no custo da chamada LLM.
WEB_SEARCH_CALL_PRICE = Decimal("0")


def _price_for(model: str) -> dict[str, Decimal]:
    # Reconhece também variantes com sufixo de data (ex.: gpt-5-mini-2025-08-07).
    for name, prices in MODEL_PRICES.items():
        if model == name or model.startswith(name + "-"):
            return prices
    return _FALLBACK_MODEL_PRICE


def estimate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    search_calls: int = 0,
) -> float:
    """Estima o custo em USD de uma chamada.

    Tokens em cache são cobrados a uma fração do preço de entrada (padrão
    trivial de 10%). A ferramenta de Web Search tem custo por chamada.
    """
    prices = _price_for(model)
    rate = Decimal("0.10")

    # cached_input_tokens normalmente é um subconjunto de input_tokens.
    # Portanto, cobramos a parte não cacheada na tarifa cheia e a parte
    # cacheada na tarifa reduzida, evitando dupla contagem.
    cached = max(0, min(int(cached_input_tokens), int(input_tokens)))
    uncached = max(0, int(input_tokens) - cached)

    total = Decimal("0")
    total += prices["input"] * Decimal(uncached) / Decimal("1000000")
    total += prices["input"] * rate * Decimal(cached) / Decimal("1000000")
    total += prices["output"] * Decimal(output_tokens) / Decimal("1000000")
    total += WEB_SEARCH_CALL_PRICE * Decimal(search_calls)

    return float(total.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP))


@dataclass
class CallRecord:
    project_id: int | None = None
    run_id: str | None = None
    operation: str | None = None
    schema_name: str | None = None
    caller: str | None = None
    model: str = ""
    success: bool = False
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    search_calls: int = 0


_context_project_id: ContextVar[int | None] = ContextVar("llm_cost_project_id", default=None)
_context_run_id: ContextVar[str | None] = ContextVar("llm_cost_run_id", default=None)
_context_operation: ContextVar[str | None] = ContextVar("llm_cost_operation", default=None)
_context_schema_name: ContextVar[str | None] = ContextVar("llm_cost_schema_name", default=None)


def set_cost_operation(operation: str | None) -> None:
    """Define a operação corrente no contexto da thread/execução."""
    _context_operation.set(operation)


def current_run_id() -> str | None:
    """Run corrente propagado pelo ``cost_context`` (usado na auditoria)."""
    return _context_run_id.get()


class cost_context:
    """Propaga o contexto de custo para chamadas feitas dentro do bloco."""

    def __init__(
        self,
        *,
        project_id: int | None = None,
        run_id: str | None = None,
        operation: str | None = None,
        schema_name: str | None = None,
    ) -> None:
        self.project_id = project_id
        self.run_id = run_id
        self.operation = operation
        self.schema_name = schema_name
        self._tokens: list[object] = []

    def __enter__(self) -> "cost_context":
        self._tokens = [
            _context_project_id.set(self.project_id),
            _context_run_id.set(self.run_id),
            _context_operation.set(self.operation),
            _context_schema_name.set(self.schema_name),
        ]
        return self

    def __exit__(self, *args: object) -> None:
        for token in reversed(self._tokens):
            token.var.reset(token)


def _is_sqlite_busy(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def record_llm_call(
    *,
    model: str,
    caller: str,
    success: bool,
    error: str | None = None,
    schema_name: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    search_calls: int = 0,
) -> None:
    """Persiste um registro de chamada usando o contexto corrente.

    A gravação abre uma conexão própria e, sob SQLite multithread, pode colidir
    com a conexão da execução (``database is locked``). Esse registro é somente
    telemetria: tentamos por alguns instantes e, se ainda assim falhar, seguimos
    sem derrubar a pipeline que acabou de gastar tokens na chamada LLM.
    """
    cost = estimate_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        search_calls=search_calls,
    )

    def row() -> LLMCall:
        return LLMCall(
            project_id=_context_project_id.get(),
            run_id=_context_run_id.get(),
            operation=_context_operation.get(),
            schema_name=schema_name or _context_schema_name.get(),
            caller=caller,
            model=model,
            success=success,
            error=((error or "")[:2000] or None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            search_calls=search_calls,
            cost_usd=cost,
        )

    max_attempts = 3
    for attempt in range(max_attempts):
        db = SessionLocal()
        try:
            db.add(row())
            db.commit()
            return
        except OperationalError as exc:
            db.rollback()
            if attempt >= max_attempts - 1 or not _is_sqlite_busy(exc):
                logger.warning("Falha ao gravar custo da chamada LLM (%s): %s", caller, exc)
                return
            time.sleep(0.5 * (attempt + 1))
        finally:
            db.close()


# Conveniência para testes: injeta um "sink" alternativo.
_record_sink = record_llm_call
_allowed_kwargs = {"model", "caller", "success", "error", "schema_name", "input_tokens", "output_tokens", "cached_input_tokens", "search_calls", "project_id", "run_id", "operation"}


def set_record_sink(sink):
    global _record_sink
    _record_sink = sink


def emit(**record) -> None:
    """Registra a chamada (usado por llm.py); testável via sink.

    O contexto corrente (projeto/run/operação/schema) é mesclado nos campos,
    permitindo que o sink serializado leia os mesmos dados sem conhecer as
    ``contextvars``. Ao chamar ``record_llm_call`` (sink padrão), os campos de
    contexto são removidos porque essa função lê as ``contextvars`` diretamente.
    """
    if "operation" not in record:
        record["operation"] = _context_operation.get()
    if "schema_name" not in record:
        record["schema_name"] = _context_schema_name.get()
    if "project_id" not in record:
        record["project_id"] = _context_project_id.get()
    if "run_id" not in record:
        record["run_id"] = _context_run_id.get()

    if _record_sink is record_llm_call:
        kwargs = {k: v for k, v in record.items() if k in _allowed_kwargs}
        kwargs.pop("project_id", None)
        kwargs.pop("run_id", None)
        kwargs.pop("operation", None)
        _record_sink(**kwargs)
    else:
        _record_sink(**record)
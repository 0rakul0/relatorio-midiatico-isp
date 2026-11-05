"""Executor limitado para atender usuarios simultaneos sem bloquear a API.

A fila e em memoria e usada somente no processo Uvicorn unico do deployment
SQLite. Cada projeto continua tendo no maximo uma execucao ativa, enquanto
ate MAX_CONCURRENT_REPORTS projetos diferentes processam em paralelo.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Lock
from typing import Callable

from app.config import get_settings
from app.orchestration.state import RunCancelled, check_cancelled, mark_run_cancelled, mark_run_failed

logger = logging.getLogger(__name__)


class RunQueueFull(RuntimeError):
    """A capacidade total de execucoes ativas + aguardando foi atingida."""


class RunScheduler:
    def __init__(self, *, concurrent: int, queued: int):
        self.concurrent = max(1, int(concurrent))
        self.queued = max(0, int(queued))
        self._slots = BoundedSemaphore(self.concurrent + self.queued)
        self._pool = ThreadPoolExecutor(
            max_workers=self.concurrent,
            thread_name_prefix="isp-report",
        )

    def reserve(self) -> None:
        if not self._slots.acquire(blocking=False):
            raise RunQueueFull(
                "Servidor ocupado: todas as vagas de pesquisa e de espera "
                "estao utilizadas. Tente novamente em instantes."
            )

    def release(self) -> None:
        self._slots.release()

    def submit_reserved(
        self,
        worker: Callable[[str, int], None],
        run_id: str,
        project_id: int,
    ) -> None:
        """Recebe uma vaga previamente reservada e libera-a ao finalizar."""
        def task() -> None:
            try:
                # Cancela uma tarefa ainda na fila sem executar qualquer etapa.
                try:
                    check_cancelled(run_id)
                except RunCancelled:
                    mark_run_cancelled(run_id)
                    return
                worker(run_id, project_id)
            except Exception as exc:
                logger.exception(
                    "Falha inesperada no executor do projeto %s (run %s)",
                    project_id, run_id,
                )
                mark_run_failed(run_id, str(exc))
            finally:
                self.release()

        try:
            self._pool.submit(task)
        except Exception:
            self.release()
            raise

    def shutdown(self, *, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=False)


_scheduler: RunScheduler | None = None
_scheduler_lock = Lock()


def get_scheduler() -> RunScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            settings = get_settings()
            _scheduler = RunScheduler(
                concurrent=settings.max_concurrent_reports,
                queued=settings.max_queued_reports,
            )
        return _scheduler

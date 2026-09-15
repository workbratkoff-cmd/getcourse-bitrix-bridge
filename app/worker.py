"""Фоновый воркер: разбирает очередь и повторяет неудачные доставки."""
from __future__ import annotations

import asyncio

from .logger import get_logger

log = get_logger("worker")


class Worker:
    def __init__(self, settings, store, processor):
        self.settings = settings.worker
        self.store = store
        self.processor = processor
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def start(self) -> None:
        requeued = self.store.requeue_stuck_jobs()
        if requeued:
            log.warning("worker.requeued_stuck_jobs", extra={"count": requeued})

        self._stopped.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("worker.started", extra={"poll_interval_sec": self.settings.poll_interval_sec})

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception as exc:  # одна ошибка не должна убить воркер
                log.error("worker.tick_failed", extra={"error": str(exc)})
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.settings.poll_interval_sec
                )
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> dict[str, int]:
        # один проход очереди; из тестов и демо вызывается вручную
        processed = 0
        failed = 0

        for job in self.store.claim_due_jobs(self.settings.batch_size):
            try:
                await self.processor.process(job)
                self.store.complete_job(job["id"])
                processed += 1
            except Exception as exc:
                failed += 1
                # PermanentError повторять нечего — сразу сдаёмся
                if getattr(exc, "retryable", True) is False:
                    outcome = self.store.kill_job(job["id"], exc)
                else:
                    outcome = self.store.fail_job(
                        job["id"], exc,
                        self.settings.backoff_base_sec,
                        self.settings.backoff_max_sec,
                    )

                log.error("job.failed", extra={
                    "job_id": job["id"],
                    "attempts": outcome["attempts"],
                    "next_status": outcome["status"],
                    "retry_after": outcome["run_after"],
                    "error": str(exc),
                })

        return {"processed": processed, "failed": failed}

"""Background audit writer — every prediction is persisted, none block the caller.

Determinism & auditability requirement: each output must be reproducible, so
we store the full inputs, model versions, prompt hash, signals and result per
job. To keep the hot path free of per-request DB writes (and SQLite free of
500 concurrent writers), records go onto an asyncio queue and a single
consumer batches them into the fusion_audit table. Enqueue is O(1) and never
blocks; if the queue is somehow full the record is dropped and counted rather
than stalling a user request.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.models import FusionAudit

logger = logging.getLogger(__name__)

_QUEUE_MAX = 10000
_BATCH_MAX = 200
_FLUSH_INTERVAL_S = 0.5


class AuditWriter:
    def __init__(self) -> None:
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped = 0
        self.written = 0

    def _ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
            self._task = asyncio.get_running_loop().create_task(self._run())

    def enqueue(self, record: FusionAudit) -> None:
        self._ensure_started()
        try:
            self._queue.put_nowait(record)
            self._idle.clear()
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = asyncio.get_running_loop().time() + _FLUSH_INTERVAL_S
            while len(batch) < _BATCH_MAX:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout))
                except asyncio.TimeoutError:
                    break
            try:
                # Resolve the engine at write time — tests swap app.db.engine.
                import app.db as db
                async with db.AsyncSession(db.engine, expire_on_commit=False) as session:
                    session.add_all(batch)
                    await session.commit()
                self.written += len(batch)
            except Exception as exc:
                self.dropped += len(batch)
                logger.error("Audit batch write failed (%d records): %s", len(batch), exc)
            if self._queue.empty():
                self._idle.set()

    async def flush(self, timeout: float = 5.0) -> None:
        """Wait until every enqueued record has been committed (tests/shutdown)."""
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            logger.warning("Audit flush timed out with %d records pending", self._queue.qsize())

    async def stop(self) -> None:
        if self._task is not None:
            await self.flush()
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def reset_for_tests(self) -> None:
        self._task = None
        self._queue = None
        self._idle = asyncio.Event()
        self._idle.set()
        self.dropped = 0
        self.written = 0

"""Tests for Feature 10: job queue backend + audit log."""

from __future__ import annotations

import asyncio
import unittest

from tests import _testdb  # noqa: F401 — sets up in-memory DB + migrations
import db  # noqa: E402

from jobs import enqueue_job, list_recent_jobs, get_worker_status, retry_job  # noqa: E402
from jobs.registry import register_job, job_registry  # noqa: E402
from jobs.backend import _ensure_jobs_table  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Register test-only jobs. Idempotent across pytest re-runs.
if "test_hello" not in job_registry:
    @register_job("test_hello")
    async def _test_hello(greet_target: str = "world") -> dict:
        return {"greeting": f"hi {greet_target}"}

if "test_fails" not in job_registry:
    @register_job("test_fails")
    async def _test_fails() -> dict:
        raise RuntimeError("deliberate failure")


class TestJobRegistry(unittest.TestCase):
    def test_known_jobs_are_registered(self):
        status = get_worker_status()
        self.assertIn("send_email", status["registered_jobs"])
        self.assertIn("send_weekly_digest_batch", status["registered_jobs"])
        self.assertIn("send_market_resolution_notifications", status["registered_jobs"])
        self.assertIn("process_scheduled_deletions", status["registered_jobs"])
        self.assertIn("generate_sitemap", status["registered_jobs"])

    def test_backend_is_in_process(self):
        status = get_worker_status()
        self.assertEqual(status["backend"], "InProcessBackend")


class TestEnqueueAndAudit(unittest.TestCase):
    def test_enqueue_writes_audit_row(self):
        _ensure_jobs_table()
        job_id = _run(enqueue_job("test_hello", greet_target="alice"))
        self.assertIsInstance(job_id, int)
        # Let the event loop run the spawned task
        _run(asyncio.sleep(0.1))
        rows = list_recent_jobs(limit=5)
        self.assertGreater(len(rows), 0)
        row = next((r for r in rows if r["id"] == job_id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "test_hello")

    def test_failing_job_retries_then_records_error(self):
        """Enqueue → retry loop → audit row reflects the final failure.

        The in-process backend spawns the worker task on the same event loop
        as the enqueue call, so this test MUST execute enqueue + polling
        inside a single coroutine. Using multiple `_run()` calls would close
        the loop between them and destroy the pending retry task.
        """
        async def scenario() -> dict:
            job_id = await enqueue_job("test_fails")
            # 2s + 4s exponential backoff for 3 attempts ≈ 6s — poll up to 8s.
            for _ in range(80):
                await asyncio.sleep(0.1)
                with db.conn() as c:
                    row = c.execute(
                        "SELECT status, attempts FROM background_jobs WHERE id = ?",
                        (job_id,),
                    ).fetchone()
                if row and row["status"] == "failed":
                    return {"status": row["status"], "attempts": row["attempts"]}
            return {"status": row["status"] if row else None, "attempts": row["attempts"] if row else 0}

        result = _run(scenario())
        self.assertEqual(result["status"], "failed")
        self.assertGreaterEqual(result["attempts"], 1)

    def test_retry_job_enqueues_again(self):
        async def scenario() -> bool:
            job_id = await enqueue_job("test_hello", greet_target="retry")
            await asyncio.sleep(0.1)
            return await retry_job(job_id)

        self.assertTrue(_run(scenario()))

    def test_retry_unknown_job_returns_false(self):
        ok = _run(retry_job(999999))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

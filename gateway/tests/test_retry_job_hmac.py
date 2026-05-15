"""HIGH-21 / Audit #14 — payload HMAC enforcement on `retry_job`.

The retry path re-dispatches arbitrary rows from `background_jobs`. Without
an HMAC check, anyone with INSERT capability on that table (a stored-RCE
pivot through any side-channel writer) could plant a row naming an
existing registered coroutine with a malicious payload and trigger it by
hitting the admin retry endpoint.

These tests pin the three guard paths:

1. Valid stamp → dispatches.
2. Tampered name/payload → False, no dispatch.
3. Missing/empty stamp (pre-migration rows or planted ones) → False,
   audit log warns, no dispatch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest

from tests import _testdb  # noqa: F401 — shared in-memory DB + migrations
import db  # noqa: E402

from jobs import enqueue_job, retry_job  # noqa: E402
from jobs.registry import (  # noqa: E402
    job_registry,
    register_job,
    compute_job_hmac,
)
from jobs.backend import _ensure_jobs_table  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Test-only job. Idempotent across pytest re-runs.
if "hmac_probe_job" not in job_registry:
    @register_job("hmac_probe_job")
    async def _hmac_probe_job(token: str = "default") -> dict:
        return {"token": token}


def _read_row(job_id: int) -> dict | None:
    with db.conn() as c:
        row = c.execute(
            "SELECT name, payload, payload_hmac FROM background_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


class RetryJobHmacEnforcement(unittest.TestCase):
    """Pin the three retry_job HMAC outcomes."""

    def setUp(self) -> None:
        _ensure_jobs_table()

    # ── Path 1: enqueue stamps a valid HMAC, retry dispatches ──────────

    def test_valid_hmac_dispatches(self):
        """Rows planted by `enqueue_job` round-trip cleanly through retry."""
        async def scenario() -> bool:
            job_id = await enqueue_job("hmac_probe_job", token="alpha")
            await asyncio.sleep(0.05)
            return await retry_job(job_id)

        self.assertTrue(_run(scenario()))

        # Sanity: the row carries a non-empty HMAC after enqueue.
        with db.conn() as c:
            row = c.execute(
                "SELECT payload_hmac FROM background_jobs "
                "WHERE name = 'hmac_probe_job' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["payload_hmac"])

    # ── Path 2: payload/name mutation invalidates the stored HMAC ──────

    def test_tampered_payload_rejected(self):
        """Mutate the row's payload after enqueue — retry must refuse.

        Simulates an attacker writing through a side channel that doesn't
        know the HMAC secret: the stored HMAC was computed against the
        original payload, so any downstream mutation breaks verification.
        """
        async def enqueue() -> int:
            return await enqueue_job("hmac_probe_job", token="original")

        job_id = _run(enqueue())
        _run(asyncio.sleep(0.05))

        # Tamper: swap the payload, leave the (now-stale) HMAC alone.
        with db.conn() as c:
            c.execute(
                "UPDATE background_jobs SET payload = ? WHERE id = ?",
                (json.dumps({"token": "hijacked"}), job_id),
            )

        with self.assertLogs("jobs", level="WARNING") as logs:
            ok = _run(retry_job(job_id))
        self.assertFalse(ok)
        self.assertTrue(
            any("HMAC verification failed" in m for m in logs.output),
            f"expected HMAC failure audit log; got {logs.output!r}",
        )

    def test_tampered_name_rejected(self):
        """Re-pointing the row at a different registered job is refused.

        The HMAC binds (name, payload) together. Swapping the name to
        another job still in the allowlist must fail verification.
        """
        # Make sure both names are registered so the failure is purely HMAC.
        if "hmac_probe_alt" not in job_registry:
            @register_job("hmac_probe_alt")
            async def _alt(**_kw):  # pragma: no cover — never dispatched
                return {"alt": True}

        async def enqueue() -> int:
            return await enqueue_job("hmac_probe_job", token="bound-to-probe")

        job_id = _run(enqueue())
        _run(asyncio.sleep(0.05))

        with db.conn() as c:
            c.execute(
                "UPDATE background_jobs SET name = 'hmac_probe_alt' WHERE id = ?",
                (job_id,),
            )

        with self.assertLogs("jobs", level="WARNING") as logs:
            ok = _run(retry_job(job_id))
        self.assertFalse(ok)
        self.assertTrue(
            any("HMAC verification failed" in m for m in logs.output),
            f"expected HMAC failure audit log; got {logs.output!r}",
        )

    # ── Path 3: empty/NULL HMAC is refused outright ────────────────────

    def test_empty_hmac_rejected_with_audit_log(self):
        """A row with no HMAC (pre-migration or planted) must be refused.

        Operators get told via the audit log so a planted row produces a
        breadcrumb instead of a silent retry.
        """
        async def enqueue() -> int:
            return await enqueue_job("hmac_probe_job", token="will-be-stripped")

        job_id = _run(enqueue())
        _run(asyncio.sleep(0.05))

        # Wipe the HMAC to mimic a pre-migration-192 legacy row or a row
        # planted via a side channel that doesn't know the signing secret.
        with db.conn() as c:
            c.execute(
                "UPDATE background_jobs SET payload_hmac = NULL WHERE id = ?",
                (job_id,),
            )

        with self.assertLogs("jobs", level="WARNING") as logs:
            ok = _run(retry_job(job_id))
        self.assertFalse(ok)
        self.assertTrue(
            any("HMAC verification failed" in m for m in logs.output),
            f"expected HMAC failure audit log; got {logs.output!r}",
        )

    def test_empty_string_hmac_rejected(self):
        """An empty-string HMAC is not a valid stamp either."""
        async def enqueue() -> int:
            return await enqueue_job("hmac_probe_job", token="empty-string-hmac")

        job_id = _run(enqueue())
        _run(asyncio.sleep(0.05))

        with db.conn() as c:
            c.execute(
                "UPDATE background_jobs SET payload_hmac = '' WHERE id = ?",
                (job_id,),
            )

        ok = _run(retry_job(job_id))
        self.assertFalse(ok)


class HmacHelperContract(unittest.TestCase):
    """Lightweight invariants on the helper that backs the retry guard."""

    def test_compute_hmac_is_deterministic(self):
        a = compute_job_hmac("hmac_probe_job", {"token": "x", "n": 1})
        b = compute_job_hmac("hmac_probe_job", {"n": 1, "token": "x"})
        self.assertEqual(a, b, "HMAC must be order-independent over payload keys")

    def test_compute_hmac_changes_with_name(self):
        a = compute_job_hmac("hmac_probe_job", {"token": "x"})
        b = compute_job_hmac("hmac_probe_alt", {"token": "x"})
        self.assertNotEqual(a, b)

    def test_compute_hmac_changes_with_payload(self):
        a = compute_job_hmac("hmac_probe_job", {"token": "x"})
        b = compute_job_hmac("hmac_probe_job", {"token": "y"})
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()

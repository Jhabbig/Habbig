"""Scheduler tests.

Scope:
  * Registration wraps user-supplied functions with run-recording.
  * A failing job is caught and recorded (ok=0), not raised.
  * Audit rows land in ``job_runs`` (migration 105).
  * ``trigger_now`` fires inside 1 second.
  * ``pause`` / ``resume`` toggle APScheduler's next_run_time.

Tests use the shared in-memory DB via ``_testdb``. The scheduler itself
is exercised in asyncio mode — APScheduler's AsyncIOScheduler needs a
running loop to tick.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from tests import _testdb  # noqa: F401 — install shared in-memory DB

USES_TESTDB = True


class TestSchedulerRegistration(unittest.TestCase):
    """Register jobs with a fresh scheduler; validate metadata is captured."""

    def _fresh_scheduler(self):
        # Instantiate a new Scheduler() (not the singleton) so tests
        # don't leak state into each other.
        from scheduler.scheduler import Scheduler
        return Scheduler()

    def test_add_interval_registers_metadata(self):
        sched = self._fresh_scheduler()

        async def noop():
            return None

        sched.add_interval("t_interval", noop, seconds=30)
        self.assertIn("t_interval", sched.jobs)
        meta = sched.jobs["t_interval"]
        self.assertEqual(meta["func_name"], "noop")
        self.assertIn("interval", meta["trigger"].lower())

    def test_add_cron_parses_five_field_expression(self):
        sched = self._fresh_scheduler()

        def fn():
            return None

        sched.add_cron("t_cron", fn, "0 7 * * 1")
        self.assertIn("t_cron", sched.jobs)

    def test_add_cron_rejects_wrong_field_count(self):
        sched = self._fresh_scheduler()
        with self.assertRaises(ValueError):
            sched.add_cron("bad", lambda: None, "0 7 *")


class TestSchedulerRuns(unittest.IsolatedAsyncioTestCase):
    """Exercise the event loop — register a job, run a tick, verify audit."""

    def setUp(self):
        import os
        # The shared conftest sets NARVE_SKIP_SCHEDULER=1 to suppress the
        # background scheduler thread during tests that don't actually
        # exercise it. This suite DOES exercise it, so temporarily pop
        # the flag so Scheduler().start() runs APScheduler's _impl.start
        # rather than returning early.
        self._prev_skip = os.environ.pop("NARVE_SKIP_SCHEDULER", None)
        from scheduler.scheduler import Scheduler
        self.sched = Scheduler()

    async def asyncTearDown(self):
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            pass
        # Restore the env so downstream tests see the same scheduler-off
        # state the conftest installed.
        import os
        if self._prev_skip is not None:
            os.environ["NARVE_SKIP_SCHEDULER"] = self._prev_skip

    def _count_runs(self, name: str) -> int:
        import db
        with db.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM job_runs WHERE job_name = ?", (name,)
            ).fetchone()[0]

    def _last_run(self, name: str) -> dict | None:
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT * FROM job_runs WHERE job_name = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            return dict(row) if row else None

    async def test_trigger_now_fires_within_one_second(self):
        fired = asyncio.Event()

        async def job():
            fired.set()

        self.sched.add_interval("t_trigger", job, seconds=3600)  # far future
        self.sched.start()
        self.sched.trigger_now("t_trigger", triggered_by="test")

        # APScheduler's modify_job schedules next_run_time=now — give it
        # a tick of headroom. If it doesn't fire in 1s, the scheduler
        # isn't actually ticking.
        await asyncio.wait_for(fired.wait(), timeout=1.0)

        # Audit row should exist and be marked ok=1 with triggered_by=test.
        # Allow up to 500ms for record_end to flush.
        for _ in range(10):
            last = self._last_run("t_trigger")
            if last and last.get("completed_at") is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(last)
        self.assertEqual(last["ok"], 1)
        self.assertEqual(last["triggered_by"], "test")

    async def test_failing_job_does_not_crash_scheduler(self):
        attempts = []

        async def flaky():
            attempts.append(1)
            raise RuntimeError("simulated failure")

        self.sched.add_interval("t_fail", flaky, seconds=3600)
        self.sched.start()
        self.sched.trigger_now("t_fail", triggered_by="test")

        # Give the scheduler up to 1s to run the broken job.
        for _ in range(20):
            if attempts:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(len(attempts), 1, "flaky job should have fired once")

        # Scheduler still alive — triggering a fresh job works.
        ok_fired = asyncio.Event()

        async def ok_job():
            ok_fired.set()

        self.sched.add_interval("t_ok_after_fail", ok_job, seconds=3600)
        self.sched.trigger_now("t_ok_after_fail", triggered_by="test")
        await asyncio.wait_for(ok_fired.wait(), timeout=1.0)

        # Failed run should be recorded with ok=0 + error message.
        for _ in range(10):
            last = self._last_run("t_fail")
            if last and last.get("completed_at") is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(last)
        self.assertEqual(last["ok"], 0)
        self.assertIn("simulated failure", last["error"] or "")

    async def test_pause_resume_flip_next_run_time(self):
        async def noop():
            return None

        self.sched.add_interval("t_toggle", noop, seconds=30)
        self.sched.start()
        meta = {j["name"]: j for j in self.sched.jobs_metadata()}["t_toggle"]
        self.assertFalse(meta["paused"])

        self.sched.pause("t_toggle")
        meta = {j["name"]: j for j in self.sched.jobs_metadata()}["t_toggle"]
        self.assertTrue(meta["paused"])

        self.sched.resume("t_toggle")
        meta = {j["name"]: j for j in self.sched.jobs_metadata()}["t_toggle"]
        self.assertFalse(meta["paused"])


class TestLegacyCronTranslation(unittest.TestCase):
    """Spot-check the arq → cron translation used by registry._cron_from_legacy."""

    def test_mon_0700_weekly(self):
        from scheduler.registry import _cron_from_legacy
        got = _cron_from_legacy({"minute": 0, "hour": 7, "weekday": 0, "day": None})
        # weekday=0 (arq Monday) -> cron dow=1
        self.assertEqual(got, "0 7 * * 1")

    def test_daily_0317(self):
        from scheduler.registry import _cron_from_legacy
        got = _cron_from_legacy({"minute": 17, "hour": 3, "weekday": None, "day": None})
        self.assertEqual(got, "17 3 * * *")

    def test_any_minute(self):
        from scheduler.registry import _cron_from_legacy
        got = _cron_from_legacy({"minute": None, "hour": None, "weekday": None, "day": None})
        self.assertEqual(got, "* * * * *")


if __name__ == "__main__":
    unittest.main()

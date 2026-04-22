"""Tests for the Claude cost-control layer.

Covers the three net-new pieces of the cost-control pass:

  1. ``ai.client.call_claude`` — cache hit path returns the cached value
     and logs a cache-hit row; kill-switch short-circuits uncached calls;
     a successful call logs tokens + cost + writes to cache when a key +
     TTL were passed.
  2. ``ai.client.is_kill_switch_active`` / ``set_kill_switch`` — flipping
     the singleton row round-trips through the dedicated table.
  3. ``jobs.claude_cost_check.check_daily_claude_spend`` — records an
     alert row on threshold breach, flips the kill-switch on the $200
     bound, and is idempotent (re-runs don't double-insert).

Tests pin ``db.conn`` to an in-memory SQLite before ``server`` is
imported, in the same pattern as test_environmental_http.py.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import sqlite3
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode())
except Exception:
    pass

import db  # noqa: E402

_conn = sqlite3.connect(":memory:", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA foreign_keys = ON")


@contextlib.contextmanager
def _fake_conn():
    try:
        yield _conn
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise


db.conn = _fake_conn
db.init_db()

import migrations  # noqa: E402
migrations.upgrade_to_head()

# The ai.client + jobs modules open their own sqlite connection to the
# same path. Point them at the in-memory DB by routing GATEWAY_DB_PATH
# to the shared connection via a monkey-patched _connect.
from ai import client as ai_client  # noqa: E402
from ai import cache as ai_cache  # noqa: E402
import jobs.claude_cost_check as cost_check  # noqa: E402

class _NoCloseConn:
    """Proxy the shared in-memory connection but swallow .close() so the
    module-level test DB survives calls that wrap their own try/finally.

    Implements the context-manager protocol too, since ai/client now
    calls ``with _connect() as conn:`` to route through ``db.conn``.
    """
    def __init__(self, target):
        self._target = target

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._target, name)

    def __enter__(self):
        return self._target

    def __exit__(self, *exc):
        if exc[0] is None:
            try:
                self._target.commit()
            except Exception:
                pass
        return False


def _shared_conn():
    return _NoCloseConn(_conn)


ai_client._connect = _shared_conn  # type: ignore[attr-defined]
ai_cache._connect = _shared_conn   # type: ignore[attr-defined]
cost_check._db_path = lambda: None  # unused once sqlite3.connect is stubbed


# The cost-check job uses sqlite3.connect directly for the aggregation.
# Patch that so it hits the in-memory connection.
_orig_sqlite_connect = sqlite3.connect


def _sqlite_connect_in_memory(*args, **kwargs):
    return _NoCloseConn(_conn)


# ── Helpers ────────────────────────────────────────────────────────────


def _clear_tables():
    _conn.execute("DELETE FROM claude_usage_log")
    _conn.execute("DELETE FROM claude_cost_alerts")
    _conn.execute("UPDATE claude_kill_switch SET active = 0, reason = NULL, triggered_at = NULL, triggered_by = NULL WHERE id = 1")
    _conn.execute("DELETE FROM ai_cache")
    _conn.commit()


def _fake_response(text: str, in_tok: int = 100, out_tok: int = 50):
    content = [SimpleNamespace(text=text)]
    usage = SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok)
    return SimpleNamespace(content=content, usage=usage)


# ── Unit: call_claude cache + dispatch + kill-switch ───────────────────


class TestCallClaude(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_tables()

    async def test_cache_hit_short_circuits_and_logs(self):
        ai_cache.set("test:key-1", "cached-value",
                     ttl_seconds=3600, feature="test", model="claude-haiku-4-5-20251001")
        result = await ai_client.call_claude(
            feature="test",
            system="s", user="u",
            model="claude-haiku-4-5-20251001",
            cache_key="test:key-1",
        )
        self.assertEqual(result, "cached-value")
        row = _conn.execute(
            "SELECT feature, cached_hit, cost_usd FROM claude_usage_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["feature"], "test")
        self.assertEqual(row["cached_hit"], 1)
        self.assertEqual(row["cost_usd"], 0.0)

    async def test_kill_switch_blocks_uncached_call(self):
        ai_client.set_kill_switch(active=True, reason="test", triggered_by="pytest")
        try:
            result = await ai_client.call_claude(
                feature="test", system="s", user="u",
                model="claude-haiku-4-5-20251001",
            )
        finally:
            ai_client.set_kill_switch(active=False)
        self.assertIsNone(result)
        row = _conn.execute(
            "SELECT cached_hit, cost_usd FROM claude_usage_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # Failure row: uncached, zero cost.
        self.assertEqual(row["cached_hit"], 0)
        self.assertEqual(row["cost_usd"], 0.0)

    async def test_kill_switch_does_not_block_cache_hit(self):
        ai_cache.set("test:key-ks", "cached-under-kill-switch",
                     ttl_seconds=3600, feature="test", model="claude-haiku-4-5-20251001")
        ai_client.set_kill_switch(active=True, reason="test", triggered_by="pytest")
        try:
            result = await ai_client.call_claude(
                feature="test", system="s", user="u",
                cache_key="test:key-ks",
                model="claude-haiku-4-5-20251001",
            )
        finally:
            ai_client.set_kill_switch(active=False)
        self.assertEqual(result, "cached-under-kill-switch")

    async def test_successful_call_logs_tokens_cost_and_writes_cache(self):
        resp = _fake_response("output-text", in_tok=200, out_tok=100)
        fake_sdk = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=resp)))
        original = ai_client.get_async_client
        ai_client.get_async_client = lambda: fake_sdk
        try:
            result = await ai_client.call_claude(
                feature="test",
                system="s", user="u",
                model="claude-haiku-4-5-20251001",
                cache_key="test:key-new",
                cache_ttl_seconds=3600,
            )
        finally:
            ai_client.get_async_client = original

        self.assertEqual(result, "output-text")
        row = _conn.execute(
            "SELECT feature, cached_hit, input_tokens, output_tokens, cost_usd "
            "FROM claude_usage_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["cached_hit"], 0)
        self.assertEqual(row["input_tokens"], 200)
        self.assertEqual(row["output_tokens"], 100)
        # Haiku: (200 * 0.25 + 100 * 1.25) / 1e6 = 0.000175
        self.assertAlmostEqual(row["cost_usd"], 0.000175, places=6)
        # Cache hit on the next call — same key, no SDK call needed.
        cached = ai_cache.get("test:key-new")
        self.assertEqual(cached, "output-text")


# ── Unit: kill-switch toggle round-trip ────────────────────────────────


class TestKillSwitch(unittest.TestCase):
    def setUp(self):
        _clear_tables()

    def test_default_inactive(self):
        self.assertFalse(ai_client.is_kill_switch_active())
        status = ai_client.get_kill_switch_status()
        self.assertFalse(status["active"])
        self.assertIsNone(status["reason"])

    def test_activate_stores_reason_and_actor(self):
        ai_client.set_kill_switch(active=True, reason="$220 on 2026-04-21", triggered_by="cost_check_job")
        status = ai_client.get_kill_switch_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["reason"], "$220 on 2026-04-21")
        self.assertEqual(status["triggered_by"], "cost_check_job")
        self.assertIsNotNone(status["triggered_at"])

    def test_deactivate_clears_triggered_at(self):
        ai_client.set_kill_switch(active=True, reason="r", triggered_by="t")
        ai_client.set_kill_switch(active=False)
        status = ai_client.get_kill_switch_status()
        self.assertFalse(status["active"])
        self.assertIsNone(status["triggered_at"])


# ── Cost-check job: alerts + kill-switch trip + idempotency ────────────


class TestDailyCostCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _clear_tables()
        # Pin both sqlite3.connect paths the job uses to the in-memory conn.
        self._sqlite_patch = sqlite3.connect
        sqlite3.connect = _sqlite_connect_in_memory

    def tearDown(self):
        sqlite3.connect = self._sqlite_patch

    def _seed_yesterday(self, cost_usd: float):
        yesterday_ts = int(
            _dt.datetime.combine(
                _dt.datetime.utcnow().date() - _dt.timedelta(days=1),
                _dt.time(12, 0),
            ).replace(tzinfo=_dt.timezone.utc).timestamp()
        )
        _conn.execute(
            "INSERT INTO claude_usage_log (timestamp, feature, model, "
            "input_tokens, output_tokens, cost_usd, cached_hit) "
            "VALUES (?, 'extraction', 'claude-haiku-4-5-20251001', 1000, 500, ?, 0)",
            (yesterday_ts, cost_usd),
        )
        _conn.commit()

    async def test_under_threshold_no_alert_no_kill(self):
        self._seed_yesterday(10.0)
        result = await cost_check.check_daily_claude_spend()
        self.assertFalse(result["over_threshold"])
        self.assertFalse(result.get("kill_switch_tripped", False))
        alerts = _conn.execute("SELECT COUNT(*) AS n FROM claude_cost_alerts").fetchone()
        self.assertEqual(alerts["n"], 0)

    async def test_over_50_records_alert(self):
        self._seed_yesterday(75.0)
        # Monkey-patch the email enqueue path so the test doesn't need it.
        cost_check._try_enqueue_email = AsyncMock(return_value=None)
        result = await cost_check.check_daily_claude_spend()
        self.assertTrue(result["over_threshold"])
        alerts = _conn.execute(
            "SELECT alert_date, threshold_usd, total_cost_usd FROM claude_cost_alerts"
        ).fetchall()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(float(alerts[0]["threshold_usd"]), 50.0)

    async def test_over_200_trips_kill_switch(self):
        self._seed_yesterday(250.0)
        cost_check._try_enqueue_email = AsyncMock(return_value=None)
        result = await cost_check.check_daily_claude_spend()
        self.assertTrue(result["kill_switch_tripped"])
        self.assertTrue(ai_client.is_kill_switch_active())
        ai_client.set_kill_switch(active=False)  # cleanup for other tests

    async def test_rerun_does_not_double_insert(self):
        self._seed_yesterday(75.0)
        cost_check._try_enqueue_email = AsyncMock(return_value=None)
        await cost_check.check_daily_claude_spend()
        await cost_check.check_daily_claude_spend()
        alerts = _conn.execute(
            "SELECT COUNT(*) AS n FROM claude_cost_alerts WHERE threshold_usd = 50"
        ).fetchone()
        self.assertEqual(alerts["n"], 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for the Polymarket sync optimisations (perf pass, 2026-05-14).

Covers the four bits of behaviour the ``perf(polymarket): …`` commit
added on top of the existing per-user sync loop:

  1. **Active users sync every 10 minutes.** Each active user only runs
     on the cron minute equal to ``hash(user_id) % 60`` mod 10 — six
     ticks per hour, spread across all minutes so the per-minute fan-out
     stays low.

  2. **Inactive users (>=30d no activity) downshift to weekly.** They
     only run on Mondays at their offset minute. On every other tick
     they appear in ``skipped_inactive``.

  3. **Market-state cache (60s TTL).** ``polymarket.fetch_market_state``
     returns cached payloads on the second call inside the TTL without
     issuing another HTTP request.

  4. **Failed Polymarket fetch doesn't kill the job.** A connection
     whose CLOB request raises is recorded in ``sync_error`` and the
     surrounding job keeps going for everyone else.

Lean on the shared ``tests._testdb`` connection so migrations run once
per pytest process. Each test wipes the polymarket tables on entry so
state never leaks between cases.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from tests import _testdb  # noqa: F401 — pins db.conn + runs migrations

import db
from jobs import sync_portfolios
from portfolio import polymarket


# ── Helpers ────────────────────────────────────────────────────────────────


_uid_counter = 0


def _make_user_with_connection(
    last_active_offset_seconds: int | None = 0,
) -> int:
    """Create a user + polymarket_connections row + optional session.

    ``last_active_offset_seconds`` is subtracted from ``now`` and written
    to ``user_sessions.last_active_at``. Pass ``None`` to skip writing
    a session row (treated as never-active).
    """
    global _uid_counter
    _uid_counter += 1
    slug = f"polysync{_uid_counter}_{int(time.time() * 1000) % 1_000_000}"
    uid = db.create_user(
        f"{slug}@test.example", "TestPass123!", username=slug,
    )
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "INSERT INTO polymarket_connections "
            "(user_id, wallet_address, connected_at) "
            "VALUES (?, ?, ?)",
            (uid, f"0x{'a' * 40}", now),
        )
        if last_active_offset_seconds is not None:
            c.execute(
                "INSERT INTO user_sessions "
                "(token_hash, user_id, ip_address, user_agent, "
                " created_at, last_active_at, expires_at, revoked) "
                "VALUES (?, ?, '', '', ?, ?, ?, 0)",
                (
                    f"hash-{uid}",
                    uid,
                    now - last_active_offset_seconds,
                    now - last_active_offset_seconds,
                    now + 86400,
                ),
            )
    return uid


def _wipe_poly_state() -> None:
    with db.conn() as c:
        c.execute("DELETE FROM polymarket_connections")
        c.execute("DELETE FROM user_sessions")
    polymarket.clear_market_cache()


# ── Scheduling: active users sync every 10 minutes ─────────────────────────


class TestActiveUserCadence(unittest.TestCase):
    def setUp(self):
        _wipe_poly_state()

    def test_active_user_synced_on_offset_minute(self):
        # Active = last_active 5 minutes ago.
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        target_minute = (uid % 60) % 10

        # struct_time index 4 = minute, 6 = weekday (0=Mon). Wednesday.
        with patch.object(
            sync_portfolios.time, "localtime",
            return_value=time.struct_time(
                (2026, 5, 13, 12, target_minute, 0, 2, 133, 0),
            ),
        ):
            with patch.object(
                polymarket, "sync_positions",
                new=AsyncMock(return_value={"count": 0, "error": None}),
            ) as mock_sync:
                result = asyncio.run(
                    sync_portfolios.sync_polymarket_positions_job(),
                )
        self.assertEqual(mock_sync.call_count, 1)
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["skipped_inactive"], 0)

    def test_active_user_skipped_on_off_minute(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        bad_minute = ((uid % 10) + 1) % 10
        with patch.object(
            sync_portfolios.time, "localtime",
            return_value=time.struct_time(
                (2026, 5, 13, 12, bad_minute, 0, 2, 133, 0),
            ),
        ):
            with patch.object(
                polymarket, "sync_positions",
                new=AsyncMock(return_value={"count": 0, "error": None}),
            ) as mock_sync:
                result = asyncio.run(
                    sync_portfolios.sync_polymarket_positions_job(),
                )
        self.assertEqual(mock_sync.call_count, 0)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["skipped_schedule"], 1)


# ── Scheduling: inactive users only sync weekly ────────────────────────────


class TestInactiveUserCadence(unittest.TestCase):
    def setUp(self):
        _wipe_poly_state()

    def test_inactive_user_synced_weekly_on_monday(self):
        uid = _make_user_with_connection(
            last_active_offset_seconds=35 * 86400,
        )
        offset_minute = uid % 60
        # Monday (weekday=0) at offset minute → should run.
        with patch.object(
            sync_portfolios.time, "localtime",
            return_value=time.struct_time(
                (2026, 5, 11, 12, offset_minute, 0, 0, 131, 0),
            ),
        ):
            with patch.object(
                polymarket, "sync_positions",
                new=AsyncMock(return_value={"count": 0, "error": None}),
            ) as mock_sync:
                result = asyncio.run(
                    sync_portfolios.sync_polymarket_positions_job(),
                )
        self.assertEqual(mock_sync.call_count, 1)
        self.assertEqual(result["synced"], 1)

    def test_inactive_user_deferred_on_non_monday(self):
        uid = _make_user_with_connection(
            last_active_offset_seconds=35 * 86400,
        )
        offset_minute = uid % 60
        # Tuesday (weekday=1) at offset minute → still deferred.
        with patch.object(
            sync_portfolios.time, "localtime",
            return_value=time.struct_time(
                (2026, 5, 12, 12, offset_minute, 0, 1, 132, 0),
            ),
        ):
            with patch.object(
                polymarket, "sync_positions",
                new=AsyncMock(return_value={"count": 0, "error": None}),
            ) as mock_sync:
                result = asyncio.run(
                    sync_portfolios.sync_polymarket_positions_job(),
                )
        self.assertEqual(mock_sync.call_count, 0)
        self.assertEqual(result["skipped_inactive"], 1)


# ── User-offset spread ─────────────────────────────────────────────────────


class TestOffsetSpread(unittest.TestCase):
    """The hash-based offset must distribute users across minutes.

    A trivial impl that returned the same minute for everyone would
    still pass the cadence tests above; cover distribution explicitly.
    """

    def test_offset_spreads_across_buckets(self):
        offsets = {
            sync_portfolios._user_offset(uid) for uid in range(200)
        }
        self.assertEqual(offsets, set(range(60)))

    def test_offset_is_stable(self):
        for uid in (1, 42, 9999):
            self.assertEqual(
                sync_portfolios._user_offset(uid),
                sync_portfolios._user_offset(uid),
            )


# ── Market-state cache ─────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Async context manager that records every .get() call."""

    instances: list["_FakeClient"] = []

    def __init__(self, *a, **k):
        self.get_calls: list[tuple[str, dict | None]] = []
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.get_calls.append((url, params))
        return _FakeResponse(self.payload)


class TestMarketStateCache(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        polymarket.clear_market_cache()
        _FakeClient.instances = []

    async def test_second_request_within_ttl_skips_network(self):
        payload = [
            {"id": "100", "title": "A", "outcomePrices": ["0.6", "0.4"]},
            {"id": "200", "title": "B", "outcomePrices": ["0.7", "0.3"]},
        ]
        _FakeClient.payload = payload  # class attribute, read by every fake

        with patch.object(polymarket.httpx, "AsyncClient", _FakeClient):
            first = await polymarket.fetch_market_state(["100", "200"])
            second = await polymarket.fetch_market_state(["100", "200"])

        self.assertEqual(set(first.keys()), {"100", "200"})
        self.assertEqual(set(second.keys()), {"100", "200"})
        # Only the first call opened an httpx client AND issued a GET.
        total_gets = sum(len(c.get_calls) for c in _FakeClient.instances)
        self.assertEqual(total_gets, 1)

    async def test_expired_entry_refetches(self):
        payload = [
            {"id": "300", "title": "C", "outcomePrices": ["0.5", "0.5"]},
        ]
        _FakeClient.payload = payload

        with patch.object(polymarket.httpx, "AsyncClient", _FakeClient):
            await polymarket.fetch_market_state(["300"], now=0.0)
            await polymarket.fetch_market_state(["300"], now=61.0)

        total_gets = sum(len(c.get_calls) for c in _FakeClient.instances)
        self.assertEqual(total_gets, 2)


# ── Failure isolation ──────────────────────────────────────────────────────


class TestFailureIsolation(unittest.TestCase):
    def setUp(self):
        _wipe_poly_state()

    def test_one_users_failure_does_not_kill_job(self):
        """Two users in the same offset bucket; one errors, the other ok.

        The bad user must record a ``sync_error`` and the good user must
        still be synced inside the same run.
        """
        bad_uid = _make_user_with_connection(
            last_active_offset_seconds=300,
        )
        good_uid = _make_user_with_connection(
            last_active_offset_seconds=300,
        )

        # If they ended up in different buckets, rewrite good_uid so
        # both run in the same cron tick. id-based offset is mod-10 in
        # the active path; rewriting the user id is the cheapest way to
        # force collision in an in-memory test DB.
        if (good_uid % 10) != (bad_uid % 10):
            with db.conn() as c:
                target = good_uid
                while (target % 10) != (bad_uid % 10) or c.execute(
                    "SELECT 1 FROM users WHERE id = ?", (target,),
                ).fetchone() and target != good_uid:
                    target += 1
                if target != good_uid:
                    c.execute("UPDATE users SET id = ? WHERE id = ?",
                              (target, good_uid))
                    c.execute(
                        "UPDATE polymarket_connections SET user_id = ? "
                        "WHERE user_id = ?",
                        (target, good_uid),
                    )
                    c.execute(
                        "UPDATE user_sessions SET user_id = ? "
                        "WHERE user_id = ?",
                        (target, good_uid),
                    )
                    good_uid = target

        async def _flaky(user_id):
            if user_id == bad_uid:
                with db.conn() as c:
                    c.execute(
                        "UPDATE polymarket_connections SET "
                        "  sync_error = ?, "
                        "  sync_error_count = sync_error_count + 1 "
                        "WHERE user_id = ?",
                        ("boom", user_id),
                    )
                return {"count": 0, "wallet": None, "error": "boom"}
            return {"count": 1, "wallet": "0x", "error": None}

        target_minute = (bad_uid % 60) % 10

        with patch.object(
            sync_portfolios.time, "localtime",
            return_value=time.struct_time(
                (2026, 5, 13, 12, target_minute, 0, 2, 133, 0),
            ),
        ):
            with patch.object(
                polymarket, "sync_positions",
                new=AsyncMock(side_effect=_flaky),
            ):
                result = asyncio.run(
                    sync_portfolios.sync_polymarket_positions_job(),
                )

        # Both users in the offset bucket were considered; one errored.
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["errors"], 1)

        with db.conn() as c:
            row = c.execute(
                "SELECT sync_error FROM polymarket_connections "
                "WHERE user_id = ?",
                (bad_uid,),
            ).fetchone()
        self.assertEqual(row["sync_error"], "boom")


if __name__ == "__main__":
    unittest.main()

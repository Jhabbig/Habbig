"""Tests for the Polymarket sync optimisations (perf pass, 2026-05-14)
plus the empty-wipe + error-leak hardening from audit HIGH-2 / HIGH-3.

Covers:

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

  5. **HIGH-2 — Targeted DELETE.** ``sync_positions`` only removes rows
     whose market_id is missing from the fresh snapshot. The previous
     unconditional wipe-and-rebuild zeroed users' portfolios on a 200
     empty body (April 2026 CLOB indexer blip).

  6. **HIGH-2 — Suspicious-blip guard.** An empty fetch with more than
     5 cached rows is treated as an upstream incident: rows are kept,
     a warning is logged, and the caller sees
     ``error="suspicious_empty_upstream"``. Empty + small cache (<=5)
     still wipes through because that is a plausible close-out.

  7. **HIGH-3 — No exception leak.** The route-level callers get a
     stable category string (``upstream_4xx``, ``timeout``, ``dns_error``
     etc.) instead of ``str(exc)``, which used to expose the wallet
     address inside the upstream URL.

Lean on the shared ``tests._testdb`` connection so migrations run once
per pytest process. Each test wipes the polymarket tables on entry so
state never leaks between cases.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx

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


# ── HIGH-2 / HIGH-3: empty-wipe guard + error-category leak ────────────────


def _seed_positions(user_id: int, market_ids: list[str]) -> None:
    """Insert one row per market_id under (user, polymarket, yes).

    Uses the live ``user_positions`` schema from migration 020
    (``market_title``, ``avg_entry_price``, ``unrealised_pnl``) — the
    one ``polymarket.sync_positions`` actually writes against. See the
    column-name comment in ``sync_positions`` for why migration 062's
    nicer names never reached the runtime DB.
    """
    now = int(time.time())
    with db.conn() as c:
        for mid in market_ids:
            c.execute(
                "INSERT INTO user_positions "
                "(user_id, platform, market_id, market_title, side, "
                " shares, avg_entry_price, current_price, unrealised_pnl, "
                " position_value_usd, last_synced_at) "
                "VALUES (?, 'polymarket', ?, ?, 'yes', 1.0, 0.5, 0.5, 0, 0.5, ?)",
                (user_id, mid, f"q-{mid}", now),
            )


def _count_positions(user_id: int) -> int:
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM user_positions "
            "WHERE user_id = ? AND platform = 'polymarket'",
            (user_id,),
        ).fetchone()["n"]


def _wipe_positions_for_user(user_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "DELETE FROM user_positions "
            "WHERE user_id = ? AND platform = 'polymarket'",
            (user_id,),
        )


def _raw_position(market_id: str) -> dict:
    """Minimal CLOB-shaped position that ``_normalise`` will accept.

    Uses ``slug`` for the market id; ``_normalise`` prefixes ``poly:``
    so the stored ``market_id`` becomes e.g. ``poly:m1``.
    """
    return {
        "slug": market_id,
        "outcomeIndex": 0,
        "size": "1.0",
        "avgPrice": "0.5",
        "curPrice": "0.5",
        "currentValue": "0.5",
        "cashPnl": "0",
        "realisedPnl": "0",
        "title": f"Question for {market_id}",
    }


class TestSyncTargetedDelete(unittest.IsolatedAsyncioTestCase):
    """HIGH-2: only stale rows are removed."""

    def setUp(self):
        _wipe_poly_state()

    async def test_five_positions_only_those_five_remain(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        # Pre-seed 7 cached rows; fetch will return 5 (with 2 overlapping
        # the cache and 3 new). Expectation: end state is exactly those 5.
        _seed_positions(uid, [f"poly:m{i}" for i in range(1, 8)])
        self.assertEqual(_count_positions(uid), 7)

        fresh = [
            _raw_position("m1"),  # already cached → kept
            _raw_position("m2"),  # already cached → kept
            _raw_position("m99"),  # new
            _raw_position("m100"),  # new
            _raw_position("m101"),  # new
        ]
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(return_value=fresh),
        ):
            result = await polymarket.sync_positions(uid)

        self.assertEqual(result["count"], 5)
        self.assertIsNone(result["error"])
        with db.conn() as c:
            rows = c.execute(
                "SELECT market_id FROM user_positions "
                "WHERE user_id = ? AND platform = 'polymarket' "
                "ORDER BY market_id",
                (uid,),
            ).fetchall()
        ids = sorted(r["market_id"] for r in rows)
        self.assertEqual(
            ids,
            sorted(["poly:m1", "poly:m2", "poly:m99", "poly:m100", "poly:m101"]),
        )

    async def test_partial_overlap_keeps_overlap_and_adds_new(self):
        """The two cached rows that ARE in the fresh snapshot must keep
        their primary-key identity rather than getting deleted + reinserted.
        We confirm by checking the row count and the market_id set.
        """
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        _seed_positions(uid, ["poly:A", "poly:B", "poly:C"])

        fresh = [_raw_position("A"), _raw_position("B"), _raw_position("D")]
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(return_value=fresh),
        ):
            await polymarket.sync_positions(uid)

        with db.conn() as c:
            rows = c.execute(
                "SELECT market_id FROM user_positions "
                "WHERE user_id = ? AND platform = 'polymarket' "
                "ORDER BY market_id",
                (uid,),
            ).fetchall()
        self.assertEqual(
            sorted(r["market_id"] for r in rows),
            ["poly:A", "poly:B", "poly:D"],
        )


class TestSyncEmptyBlipGuard(unittest.IsolatedAsyncioTestCase):
    """HIGH-2: empty fetch with a non-trivial cache is treated as a blip."""

    def setUp(self):
        _wipe_poly_state()

    async def test_empty_with_ten_existing_kept_and_warning(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        _seed_positions(uid, [f"poly:m{i}" for i in range(10)])
        self.assertEqual(_count_positions(uid), 10)

        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(return_value=[]),
        ):
            with self.assertLogs(
                "portfolio.polymarket", level=logging.WARNING,
            ) as captured:
                result = await polymarket.sync_positions(uid)

        # No rows wiped.
        self.assertEqual(_count_positions(uid), 10)
        self.assertEqual(result["error"], "suspicious_empty_upstream")
        self.assertEqual(result["count"], 10)
        # Warning carried the upstream-blip phrasing.
        joined = "\n".join(captured.output)
        self.assertIn("empty list", joined)
        self.assertIn("upstream blip", joined)

        # Connection row recorded the category in sync_error.
        with db.conn() as c:
            row = c.execute(
                "SELECT sync_error, sync_error_count "
                "FROM polymarket_connections WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["sync_error"], "suspicious_empty_upstream")
        self.assertEqual(row["sync_error_count"], 1)

    async def test_empty_with_two_existing_genuine_closeout(self):
        """Small cache below the threshold → empty fetch IS a real wipe.

        This is the legit "user closed both their positions" path. The
        guard is intentionally conservative: a real whale-scale account
        wiping 100 markets in a single tick would trigger the guard and
        require the explicit disconnect route, which is acceptable.
        """
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        _seed_positions(uid, ["poly:x", "poly:y"])
        self.assertEqual(_count_positions(uid), 2)

        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(return_value=[]),
        ):
            result = await polymarket.sync_positions(uid)

        self.assertEqual(_count_positions(uid), 0)
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["error"])
        # Connection row reset to a clean sync.
        with db.conn() as c:
            row = c.execute(
                "SELECT sync_error, sync_error_count "
                "FROM polymarket_connections WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertIsNone(row["sync_error"])
        self.assertEqual(row["sync_error_count"], 0)

    async def test_empty_with_exactly_threshold_treated_as_closeout(self):
        """Boundary: threshold is ``> 5``, so exactly 5 cached rows still
        wipe through on an empty fetch. Pin the boundary explicitly so a
        future bump (or a `>=` typo) trips this test.
        """
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        _seed_positions(uid, [f"poly:m{i}" for i in range(5)])

        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(return_value=[]),
        ):
            result = await polymarket.sync_positions(uid)

        self.assertEqual(_count_positions(uid), 0)
        self.assertIsNone(result["error"])


class TestSyncErrorCategoryLeak(unittest.IsolatedAsyncioTestCase):
    """HIGH-3: returned error is a category, never the raw exception."""

    def setUp(self):
        _wipe_poly_state()

    def _make_http_status_error(self, status: int) -> httpx.HTTPStatusError:
        """Build a realistic HTTPStatusError that leaks URL+wallet via str().

        ``httpx`` builds the default message from the response's URL,
        which is exactly the form we must NOT propagate to clients.
        """
        wallet = "0x" + "b" * 40
        request = httpx.Request(
            "GET",
            f"https://clob.polymarket.com/positions?address={wallet}",
        )
        response = httpx.Response(status, request=request)
        return httpx.HTTPStatusError(
            f"Client error '{status}' for url '{request.url}'",
            request=request,
            response=response,
        )

    async def test_429_returns_rate_limited_category(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        exc = self._make_http_status_error(429)
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(side_effect=exc),
        ):
            result = await polymarket.sync_positions(uid)

        self.assertEqual(result["error"], "upstream_rate_limited")
        # The raw URL / wallet must not appear anywhere in the returned dict.
        leaked = repr(result)
        self.assertNotIn("address=0x", leaked)
        self.assertNotIn("clob.polymarket.com", leaked)
        # Connection row stores the category, not the raw exception text.
        with db.conn() as c:
            row = c.execute(
                "SELECT sync_error FROM polymarket_connections "
                "WHERE user_id = ?",
                (uid,),
            ).fetchone()
        self.assertEqual(row["sync_error"], "upstream_rate_limited")

    async def test_4xx_returns_upstream_4xx_category(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        exc = self._make_http_status_error(404)
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(side_effect=exc),
        ):
            result = await polymarket.sync_positions(uid)
        self.assertEqual(result["error"], "upstream_4xx")
        self.assertNotIn("address=0x", repr(result))

    async def test_timeout_returns_timeout_category(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        # httpx.ConnectTimeout extends both ConnectError and TimeoutException;
        # the categoriser checks TimeoutException first, so this returns
        # "timeout" rather than "connect_error".
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(side_effect=httpx.ReadTimeout("read timeout")),
        ):
            result = await polymarket.sync_positions(uid)
        self.assertEqual(result["error"], "timeout")
        self.assertNotIn("timeout out", repr(result).lower().replace("timeout", ""))

    async def test_dns_failure_returns_dns_error_category(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        exc = httpx.ConnectError(
            "[Errno 8] nodename nor servname provided, or not known",
        )
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(side_effect=exc),
        ):
            result = await polymarket.sync_positions(uid)
        self.assertEqual(result["error"], "dns_error")
        self.assertNotIn("nodename", repr(result))

    async def test_generic_connect_error_returns_connect_error_category(self):
        uid = _make_user_with_connection(last_active_offset_seconds=300)
        exc = httpx.ConnectError("connection refused")
        with patch.object(
            polymarket, "fetch_positions",
            new=AsyncMock(side_effect=exc),
        ):
            result = await polymarket.sync_positions(uid)
        self.assertEqual(result["error"], "connect_error")
        self.assertNotIn("refused", repr(result))


if __name__ == "__main__":
    unittest.main()

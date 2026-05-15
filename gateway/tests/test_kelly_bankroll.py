"""Tests for the CRIT-1 / HIGH-1 / HIGH-3 fixes in
``audits/audit_kelly.md``.

Three concerns covered:

  1. **Canonical column** — ``POST /api/kelly/bankroll`` must land in
     ``users.bankroll`` (the column the rest of the codebase reads),
     not in the dropped ``users.bankroll_usd``. Verified by writing
     via the route, then reading back through
     ``kelly.sizing_table`` (which calls ``kelly.get_user_bankroll``)
     and asserting the value round-trips.

  2. **NaN/Inf bypass** — ``kelly.sizing_table`` must coerce NaN /
     +/-Inf inputs to the zero-bankroll response so the JSON payload
     never carries literal ``NaN`` / ``Infinity`` tokens (browsers'
     ``JSON.parse`` rejects them). Verified by passing each bad value
     into ``sizing_table`` directly and asserting the response has the
     zero-bankroll shape (``stake_usd == 0``, ``note`` set).

  3. **Migration 195 backfill** — when a row had
     ``users.bankroll IS NULL`` and ``users.bankroll_usd > 0``, the
     migration must copy the USD column into the canonical column.
     Verified by spinning up a fresh sqlite DB, running the migrations
     up to 194, planting a legacy row with the split-brain shape, then
     running migration 195 and asserting (a) the canonical column now
     carries the value, (b) the ``bankroll_usd`` column is gone.

The first two tests use the shared in-memory test DB (``_testdb``).
The third uses its own tmpfile DB so it can exercise the rebuild dance
in isolation — the shared DB doesn't have ``bankroll_usd`` anymore
after 195 runs at import-time, so the migration becomes a no-op there.
"""

from __future__ import annotations

import contextlib
import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest

# Import the shared test DB before any ``server``/``db`` imports so
# the in-memory conn is pinned. Mirrors test_portfolio_integration.py.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import _testdb  # noqa: F401, E402

USES_TESTDB = True

import db  # noqa: E402

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portfolio import kelly  # noqa: E402

client = TestClient(server.app)


# ── helpers ────────────────────────────────────────────────────────────────


_uniq = 0


def _next_slug(prefix: str) -> str:
    global _uniq
    _uniq += 1
    return f"{prefix}{_uniq}_{int(time.time())}"


def _make_trader_user() -> tuple[int, str]:
    """Create a user + hardened session.

    ``narve_session`` (returned by ``db.create_user_session``) is the
    cookie ``portfolio.routes`` reads via the hardened
    ``auth.middleware.SessionMiddleware``; the legacy
    ``pm_gateway_session`` flow doesn't reach those handlers, so the
    trading-addon gate would 401 every call. Mirrors the pattern in
    ``test_trading_addon_gate._make_user``.
    """
    slug = _next_slug("kb")
    uid = db.create_user(f"{slug}@test.example", "TestPass123!", username=slug)
    db.set_trading_addon(uid, True, period_end=int(time.time()) + 30 * 86400)
    token = db.create_user_session(uid)
    return uid, token


def _prime_csrf(token: str) -> str:
    """Prime the CSRF cookie pair so JSON POSTs sail past the middleware.

    Same pattern as ``test_trading_addon_gate._prime_csrf`` — the
    gateway's CSRF middleware requires a matching ``_csrf`` cookie +
    ``X-CSRF-Token`` header on mutating routes.
    """
    client.get(
        "/feedback",
        cookies={"narve_session": token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post_json(path: str, token: str, body: dict):
    csrf = _prime_csrf(token)
    return client.post(
        path,
        cookies={"narve_session": token, "_csrf": csrf},
        headers={"X-CSRF-Token": csrf},
        json=body,
    )


# ── 1. canonical column round-trip ─────────────────────────────────────────


class TestBankrollCanonicalColumn(unittest.TestCase):
    """CRIT-1 — POST /api/kelly/bankroll must write the same column
    kelly.get_user_bankroll (and the rest of the codebase) reads."""

    def setUp(self):
        self.uid, self.token = _make_trader_user()
        client.cookies.clear()

    def test_set_bankroll_via_route_round_trips_through_kelly(self):
        """End-to-end: route -> users.bankroll -> kelly.get_user_bankroll."""
        r = _post_json(
            "/api/kelly/bankroll", self.token, {"bankroll_usd": 7_500},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["bankroll_usd"], 7_500)

        # The module reads from users.bankroll. If the route mistakenly
        # writes the dropped bankroll_usd column we'd get 0 back.
        from_kelly = kelly.get_user_bankroll(self.uid)
        self.assertAlmostEqual(from_kelly, 7_500, places=2)

    def test_set_bankroll_via_route_reaches_canonical_db_helper(self):
        """The canonical db.get_user_bankroll (dict shape) must also see
        the route's write — that's the column server.py renderers and
        market_routes.py read."""
        r = _post_json(
            "/api/kelly/bankroll", self.token, {"bankroll_usd": 12_345},
        )
        self.assertEqual(r.status_code, 200)
        info = db.get_user_bankroll(self.uid)
        self.assertIsNotNone(info["bankroll"])
        self.assertAlmostEqual(info["bankroll"], 12_345, places=2)

    def test_kelly_sizing_table_reads_route_value(self):
        """Top-line audit assertion: a user who sets bankroll via the
        Kelly bankroll endpoint sees that bankroll fed into the Kelly
        sizing table on the next call."""
        r = _post_json(
            "/api/kelly/bankroll", self.token, {"bankroll_usd": 4_000},
        )
        self.assertEqual(r.status_code, 200)
        bankroll = kelly.get_user_bankroll(self.uid)
        table = kelly.sizing_table(0.6, 0.5, bankroll, max_cap=1.0)
        # Bankroll round-tripped into the table response.
        self.assertAlmostEqual(table["bankroll_usd"], 4_000, places=2)
        # And a non-zero stake came out — proves the value reached the
        # math, not just the response envelope.
        self.assertGreater(table["full"]["stake_usd"], 0)


# ── 2. NaN / Inf bypass at sizing_table ────────────────────────────────────


class TestSizingTableRejectsNonFinite(unittest.TestCase):
    """HIGH-1 — sizing_table must filter NaN/+/-Inf so the JSON
    response is JSON.parse-safe and downstream math doesn't poison
    every numeric field."""

    def _assert_zero_bankroll_shape(self, table: dict) -> None:
        self.assertEqual(table["bankroll_usd"], 0.0)
        self.assertEqual(table["full_kelly_pct"], 0.0)
        for row in ("full", "half", "quarter"):
            self.assertEqual(table[row]["stake_usd"], 0.0)
            self.assertEqual(table[row]["max_profit_usd"], 0.0)
            self.assertEqual(table[row]["max_loss_usd"], 0.0)
        self.assertIn("note", table)
        # No NaN/Inf leaked into the response.
        for key in ("bankroll_usd", "edge_pct", "full_kelly_pct"):
            v = table[key]
            self.assertTrue(math.isfinite(v), f"{key}={v!r} not finite")
        for row in ("full", "half", "quarter"):
            for k, v in table[row].items():
                self.assertTrue(
                    math.isfinite(v),
                    f"{row}.{k}={v!r} not finite",
                )

    def test_nan_bankroll_returns_zero_response(self):
        table = kelly.sizing_table(0.6, 0.5, float("nan"))
        self._assert_zero_bankroll_shape(table)

    def test_positive_inf_bankroll_returns_zero_response(self):
        table = kelly.sizing_table(0.6, 0.5, float("inf"))
        self._assert_zero_bankroll_shape(table)

    def test_negative_inf_bankroll_returns_zero_response(self):
        table = kelly.sizing_table(0.6, 0.5, float("-inf"))
        self._assert_zero_bankroll_shape(table)

    def test_nan_our_prob_returns_zero_response(self):
        table = kelly.sizing_table(float("nan"), 0.5, 10_000)
        self._assert_zero_bankroll_shape(table)

    def test_nan_market_prob_returns_zero_response(self):
        table = kelly.sizing_table(0.6, float("nan"), 10_000)
        self._assert_zero_bankroll_shape(table)

    def test_zero_bankroll_still_returns_zero_response(self):
        # Regression: the legacy ``bankroll <= 0`` guard must still fire.
        table = kelly.sizing_table(0.6, 0.5, 0)
        self._assert_zero_bankroll_shape(table)


# ── 3. migration 195 backfill + drop ───────────────────────────────────────


class TestMigration195Backfill(unittest.TestCase):
    """Migration 195 must:
      * copy non-NULL bankroll_usd into bankroll where bankroll IS NULL,
      * leave already-populated bankroll values untouched,
      * drop the bankroll_usd column.
    Uses a fresh tmpfile DB so we control the pre-195 state directly.
    """

    def setUp(self):
        # Tempfile sqlite — not the shared in-memory test DB, because
        # the shared DB has already had 195 applied at import time and
        # no longer carries the bankroll_usd column.
        fd, self.path = tempfile.mkstemp(prefix="kelly_mig195_", suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Build minimal pre-195 users table — enough columns to
        # exercise the rebuild path (PK + a couple of cols + both
        # bankroll columns). The real users table has dozens of cols
        # but the rebuild copies them all via PRAGMA snapshot, so the
        # essence is correctly reproduced with a small shape.
        self.conn.execute(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "email TEXT, "
            "username TEXT, "
            "bankroll REAL, "
            "kelly_fraction REAL NOT NULL DEFAULT 0.5, "
            "bankroll_usd REAL NOT NULL DEFAULT 0"
            ")"
        )
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _run_migration(self) -> None:
        # Import the migration module directly so we don't have to wire
        # the migration runner against this throwaway DB.
        from migrations import _195_drop_bankroll_usd  # type: ignore  # noqa
        # Pytest's collection happens before importlib has resolved this
        # name; fall back to importlib for safety.

    def _apply_195(self) -> None:
        import importlib
        mod = importlib.import_module("migrations.195_drop_bankroll_usd")
        # Wrap in a transaction so the rebuild has a clean rollback path
        # — mirrors how the production runner wraps each migration.
        try:
            mod.upgrade(self.conn)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _cols(self, table: str) -> set:
        return {
            row["name"]
            for row in self.conn.execute(f"PRAGMA table_info({table})")
        }

    def test_backfills_null_bankroll_from_bankroll_usd(self):
        # User wrote bankroll via the (legacy) Kelly endpoint only —
        # bankroll is NULL, bankroll_usd carries the real value.
        self.conn.execute(
            "INSERT INTO users (email, username, bankroll, bankroll_usd) "
            "VALUES ('a@x', 'a', NULL, 8800)"
        )
        self.conn.commit()
        self._apply_195()
        row = self.conn.execute(
            "SELECT bankroll FROM users WHERE email='a@x'"
        ).fetchone()
        self.assertAlmostEqual(float(row["bankroll"]), 8800, places=2)
        # bankroll_usd column should be gone.
        self.assertNotIn("bankroll_usd", self._cols("users"))

    def test_preserves_existing_bankroll_when_both_columns_populated(self):
        # User wrote to both surfaces — the canonical column wins.
        self.conn.execute(
            "INSERT INTO users (email, username, bankroll, bankroll_usd) "
            "VALUES ('b@x', 'b', 5000, 9999)"
        )
        self.conn.commit()
        self._apply_195()
        row = self.conn.execute(
            "SELECT bankroll FROM users WHERE email='b@x'"
        ).fetchone()
        self.assertAlmostEqual(float(row["bankroll"]), 5000, places=2)

    def test_leaves_unset_bankroll_null_when_usd_is_zero(self):
        # bankroll_usd defaulted to 0 for every legacy row — that's the
        # "unset" sentinel, not a real value. We must NOT copy 0 into
        # bankroll because the UI uses NULL to render "Set a bankroll".
        self.conn.execute(
            "INSERT INTO users (email, username, bankroll, bankroll_usd) "
            "VALUES ('c@x', 'c', NULL, 0)"
        )
        self.conn.commit()
        self._apply_195()
        row = self.conn.execute(
            "SELECT bankroll FROM users WHERE email='c@x'"
        ).fetchone()
        self.assertIsNone(row["bankroll"])

    def test_drops_bankroll_usd_column(self):
        # Idempotent rebuild — bankroll_usd is gone regardless of data
        # shape.
        self.conn.execute(
            "INSERT INTO users (email, username, bankroll, bankroll_usd) "
            "VALUES ('d@x', 'd', 1000, 1000)"
        )
        self.conn.commit()
        self._apply_195()
        self.assertNotIn("bankroll_usd", self._cols("users"))
        # Canonical column survives.
        self.assertIn("bankroll", self._cols("users"))

    def test_idempotent_second_run_is_noop(self):
        self.conn.execute(
            "INSERT INTO users (email, username, bankroll, bankroll_usd) "
            "VALUES ('e@x', 'e', NULL, 222)"
        )
        self.conn.commit()
        self._apply_195()
        # Second invocation: bankroll_usd no longer exists, so the
        # migration should bail before any work.
        self._apply_195()
        row = self.conn.execute(
            "SELECT bankroll FROM users WHERE email='e@x'"
        ).fetchone()
        self.assertAlmostEqual(float(row["bankroll"]), 222, places=2)


if __name__ == "__main__":
    unittest.main()

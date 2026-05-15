"""Throttle coverage for ``POST /api/portfolio/kalshi/connect``.

The audit (#14 HIGH) found that the Kalshi connect handler in
``gateway/portfolio/routes.py`` was reachable at the rate of the
``with_idempotency`` 10-second debounce (≈360 calls/h per user) against
arbitrary victim emails. A paying attacker could spray Kalshi credentials
through narve's outbound IP reputation and turn the gateway into a
credential-stuffing amplifier.

The fix stacks three independent rate-limit buckets in front of the
upstream Kalshi login call:

  * ``kalshi_connect_target:<email>``  — 5 / hour per target email
  * ``kalshi_connect_user:<uid>``      — 10 / hour per narve user
  * ``kalshi_connect_ip:<client_ip>``  — 30 / 10 min per source IP

This file pins the spec: each bucket is hit independently, each returns
HTTP 429 with a ``Retry-After`` header, and a request that does NOT trip
any bucket flows through to the (stubbed) upstream login. Mirrors the
db-pin scaffolding used by ``test_trading_addon_gate`` /
``test_portfolio_integration`` so the suite slots into the shared pytest
session without re-patching ``db.conn``.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

# Pin db.conn BEFORE importing server — required so the in-memory DB sees
# migrations before any module-level lookups.
os.environ.pop("SITE_ACCESS_TOKEN", None)
os.environ.pop("PRODUCTION", None)
os.environ["RATE_LIMIT_ENABLED"] = "true"
# Global per-IP middleware (600/min in prod) would otherwise eat our
# back-to-back requests; what we're asserting are the Kalshi-specific
# buckets, not the global cap, so push that ceiling well clear.
os.environ["GLOBAL_RATE_LIMIT_PER_MIN"] = "10000"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cryptography.fernet import Fernet
    os.environ.setdefault(
        "CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode(),
    )
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

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portfolio import kalshi as _kalshi_mod  # noqa: E402


client = TestClient(server.app)


# ── Helpers ────────────────────────────────────────────────────────────────


_unique_ctr = 0


def _unique(prefix: str) -> str:
    global _unique_ctr
    _unique_ctr += 1
    return f"{prefix}{_unique_ctr}_{int(time.time())}"


def _make_trader_user() -> tuple[int, str]:
    """Create a Trading-Add-on user + hardened session. The gate has to
    pass before the throttle ever fires, so every throttle test runs as
    an addon-active user."""
    slug = _unique("kt")
    uid = db.create_user(f"{slug}@test.example", "TestPass123!", username=slug)
    db.set_trading_addon(uid, True, period_end=int(time.time()) + 30 * 86400)
    raw = db.create_user_session(uid)
    return uid, raw


def _prime_csrf(token: str) -> str:
    """Get a CSRF cookie on the TestClient and return its value.
    POST /api/portfolio/kalshi/connect is gated by CSRFMiddleware before
    the route even runs, so the double-submit pair must line up."""
    client.get(
        "/feedback",
        cookies={"narve_session": token},
        follow_redirects=False,
    )
    return client.cookies.get("_csrf") or ""


def _post(
    token: str,
    *,
    email: str,
    ip: str = "203.0.113.7",
):
    """POST to ``/api/portfolio/kalshi/connect`` with session + CSRF set.

    The ``ip`` arg sets the ``cf-connecting-ip`` header for completeness,
    but TestClient's peer host (``testclient``) is NOT in
    ``server._TRUSTED_PROXY_HOSTS``, so the route's ``_get_client_ip``
    returns ``testclient`` regardless. Per-IP-bucket tests patch
    ``_get_client_ip`` directly (see ``_with_ip``) — the header is kept
    here only to document the production-equivalent transport layer.
    """
    csrf = _prime_csrf(token)
    return client.post(
        "/api/portfolio/kalshi/connect",
        cookies={"narve_session": token, "_csrf": csrf},
        headers={
            "X-CSRF-Token": csrf,
            "cf-connecting-ip": ip,
        },
        json={"email": email, "password": "irrelevant-stubbed"},
    )


@contextlib.contextmanager
def _with_ip(ip: str):
    """Patch ``server._get_client_ip`` to return *ip*.

    TestClient connects from peer host ``testclient`` which is not in
    ``_TRUSTED_PROXY_HOSTS``, so the cf-connecting-ip header is ignored
    and every request would otherwise be bucketed under ``testclient``.
    Patching the helper lets us exercise the per-IP bucket with distinct
    synthetic addresses — production transport (CF tunnel + loopback
    peer + cf-connecting-ip) is covered by the gateway's own IP unit
    tests, not by this throttle file.
    """
    with patch.object(server, "_get_client_ip", return_value=ip):
        yield


@contextlib.contextmanager
def _stub_kalshi_login():
    """Stub ``kalshi.login`` so a successful path returns 200 without the
    test reaching out to the live Kalshi API."""
    async def _ok(_email, _password):
        return {
            "token": "fake.kalshi.token",
            "member_id": "m-1",
            "expires_at": int(time.time()) + 3600,
        }
    with patch.object(_kalshi_mod, "login", new=AsyncMock(side_effect=_ok)):
        yield


def _clear_rate_store() -> None:
    """Wipe the in-memory rate-limit dict. Conftest's autouse fixture
    handles this between tests too, but explicit reset in setUp guards
    against single-file runs without conftest scope."""
    try:
        server._rate_store.clear()
    except Exception:
        pass


# ── Per-target-email bucket — 5 / hour ─────────────────────────────────────


class TestKalshiThrottleTargetEmailBucket(unittest.TestCase):
    """``kalshi_connect_target:<email>`` — 5 attempts per hour.

    Spec: a single victim email cannot be probed more than five times
    per hour, regardless of how many narve users / source IPs the
    attempts come from. Five fits the "honest user retries after typo"
    pattern; the 6th attempt within the window must 429.
    """

    def setUp(self):
        _clear_rate_store()
        client.cookies.clear()
        self.uid, self.token = _make_trader_user()

    def tearDown(self):
        _clear_rate_store()

    def test_429_on_sixth_attempt_same_email(self):
        target = f"{_unique('victim')}@kalshi.example"
        with _stub_kalshi_login():
            # First five attempts must pass the throttle. Their HTTP
            # status depends on the stub (200) — the assertion below
            # is that they aren't 429.
            for i in range(5):
                r = _post(self.token, email=target)
                self.assertNotEqual(
                    r.status_code, 429,
                    msg=f"attempt {i + 1}/5 unexpectedly 429: {r.text}",
                )
            # Sixth attempt within the hour trips the per-target bucket.
            r = _post(self.token, email=target)
        self.assertEqual(r.status_code, 429, msg=r.text)
        self.assertIn("Retry-After", r.headers)
        # Retry-After must be a positive integer; the email bucket uses
        # a 1-hour window, so 3600 is the expected echo.
        self.assertEqual(r.headers["Retry-After"], "3600")

    def test_different_targets_dont_share_bucket(self):
        """Five attempts at email A must NOT count toward email B."""
        with _stub_kalshi_login():
            target_a = f"{_unique('a')}@kalshi.example"
            for _ in range(5):
                _post(self.token, email=target_a)
            # Sixth at A would 429; first at B should still flow.
            # BUT — the per-narve-user bucket (10/h) is also accumulating.
            # Five attempts at A leave 5 slots before the user bucket
            # trips, which is exactly enough for one B attempt.
            target_b = f"{_unique('b')}@kalshi.example"
            r = _post(self.token, email=target_b)
        self.assertNotEqual(r.status_code, 429, msg=r.text)


# ── Per-narve-user bucket — 10 / hour ──────────────────────────────────────


class TestKalshiThrottleUserBucket(unittest.TestCase):
    """``kalshi_connect_user:<uid>`` — 10 attempts per hour per user.

    Spec: a single compromised paying session cannot be weaponised as a
    spray channel by rotating the victim email. Even if every attempt
    targets a different Kalshi account, the user-keyed bucket caps the
    total at ten per hour.
    """

    def setUp(self):
        _clear_rate_store()
        client.cookies.clear()
        self.uid, self.token = _make_trader_user()

    def tearDown(self):
        _clear_rate_store()

    def test_429_on_eleventh_attempt_rotating_emails(self):
        with _stub_kalshi_login():
            # Ten attempts across ten unique targets — clears the
            # per-target bucket on every shot (1 hit each), so only the
            # per-user bucket is in play.
            for i in range(10):
                target = f"{_unique('spray')}@kalshi.example"
                r = _post(self.token, email=target)
                self.assertNotEqual(
                    r.status_code, 429,
                    msg=f"attempt {i + 1}/10 unexpectedly 429: {r.text}",
                )
            # Eleventh on a fresh email — per-target says yes, per-user says no.
            r = _post(self.token, email=f"{_unique('spray')}@kalshi.example")
        self.assertEqual(r.status_code, 429, msg=r.text)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.headers["Retry-After"], "3600")

    def test_different_users_dont_share_bucket(self):
        with _stub_kalshi_login():
            # Burn through user A's full quota.
            for _ in range(10):
                _post(
                    self.token,
                    email=f"{_unique('exhaust')}@kalshi.example",
                )
            # User B starts fresh and should pass.
            _uid_b, token_b = _make_trader_user()
            r = _post(token_b, email=f"{_unique('fresh')}@kalshi.example")
        self.assertNotEqual(r.status_code, 429, msg=r.text)


# ── Per-source-IP bucket — 30 / 10 min ─────────────────────────────────────


class TestKalshiThrottleIpBucket(unittest.TestCase):
    """``kalshi_connect_ip:<client_ip>`` — 30 attempts per 10 minutes.

    Spec: a single attacker IP that rotates *both* compromised user
    sessions AND victim emails must still be capped. The IP-keyed bucket
    is the last-resort ceiling that catches the "many accounts, many
    emails, one network" pattern.

    The window is wider than the others (10 minutes vs 1 hour) but the
    limit is also higher (30 vs 5/10), so an honest household NAT shared
    by a couple of users isn't punished.
    """

    def setUp(self):
        _clear_rate_store()
        client.cookies.clear()

    def tearDown(self):
        _clear_rate_store()

    def test_429_on_thirty_first_attempt_same_ip(self):
        attacker_ip = "198.51.100.42"
        with _stub_kalshi_login(), _with_ip(attacker_ip):
            # Rotate users + emails on every attempt so only the IP
            # bucket is accumulating. Per-user cap is 10/h, so we need
            # at least 3 distinct users to reach 30 without tripping the
            # user bucket; four for headroom.
            users = [_make_trader_user()[1] for _ in range(4)]
            shots = 0
            for token in users:
                # 8 shots per user — under the 10/h user cap.
                for _ in range(8):
                    target = f"{_unique('ipspray')}@kalshi.example"
                    r = _post(token, email=target)
                    self.assertNotEqual(
                        r.status_code, 429,
                        msg=(
                            f"attempt {shots + 1} unexpectedly 429 "
                            f"(per-IP should not trip yet): {r.text}"
                        ),
                    )
                    shots += 1
                    if shots == 30:
                        break
                if shots == 30:
                    break
            # 31st attempt from the same IP — fresh user, fresh email,
            # only the IP bucket can be the culprit.
            fresh_uid, fresh_token = _make_trader_user()
            r = _post(
                fresh_token,
                email=f"{_unique('ipspray')}@kalshi.example",
            )
        self.assertEqual(r.status_code, 429, msg=r.text)
        self.assertIn("Retry-After", r.headers)
        # IP bucket window is 600 s.
        self.assertEqual(r.headers["Retry-After"], "600")

    def test_different_ips_dont_share_bucket(self):
        with _stub_kalshi_login():
            # Burn through one IP's quota.
            attacker_ip = "198.51.100.99"
            tokens = [_make_trader_user()[1] for _ in range(4)]
            with _with_ip(attacker_ip):
                shots = 0
                for token in tokens:
                    for _ in range(8):
                        _post(
                            token,
                            email=f"{_unique('ipA')}@kalshi.example",
                        )
                        shots += 1
                        if shots == 30:
                            break
                    if shots == 30:
                        break
            # A different IP should still pass — fresh user too so we
            # never approach the per-user cap.
            _uid_b, token_b = _make_trader_user()
            with _with_ip("203.0.113.200"):
                r = _post(
                    token_b,
                    email=f"{_unique('ipB')}@kalshi.example",
                )
        self.assertNotEqual(r.status_code, 429, msg=r.text)


# ── Happy path: clean request passes through to the upstream stub ──────────


class TestKalshiThrottleHappyPath(unittest.TestCase):
    """A single connect attempt by a fresh user against a fresh email
    from a fresh IP MUST flow through to the stubbed Kalshi login and
    return 200. This guards against a regression that wires the buckets
    so aggressively that any request 429s."""

    def setUp(self):
        _clear_rate_store()
        client.cookies.clear()
        self.uid, self.token = _make_trader_user()

    def tearDown(self):
        _clear_rate_store()

    def test_clean_request_returns_200(self):
        with _stub_kalshi_login():
            r = _post(
                self.token,
                email=f"{_unique('clean')}@kalshi.example",
                ip="203.0.113.1",
            )
        self.assertEqual(r.status_code, 200, msg=r.text)
        self.assertTrue(r.json().get("connected"))


if __name__ == "__main__":
    unittest.main()

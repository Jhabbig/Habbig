"""Tests for the outbound webhooks subsystem.

Covers:
  - db CRUD: create / list_for_user / get / deactivate / delete
  - list_active_webhooks_for_event filters by event AND by is_active
  - HMAC signature matches hmac.new(secret, "<ts>.<body>") → sha256
  - HMAC signature round-trips through verify_signature()
  - Anti-replay: stale timestamps rejected
  - 200 → no retry, success path
  - 503 → retried 3x, then DLQ + consecutive-failures bump
  - 404 → no retry, immediate DLQ (client-error short-circuit)
  - 10 consecutive failures → circuit breaker opens with 1h cooldown
  - reset on success closes the breaker
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
import unittest
from unittest import mock

from tests import _testdb  # noqa: F401

import db
import webhooks


def _mk_user(email: str) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0])


async def _no_sleep(_):
    """Async stub for asyncio.sleep so retry tests run in microseconds.

    Must be `async def` — `_deliver_with_retries` awaits the result, and a
    plain return of None would blow up with "NoneType can't be used in
    await expression"."""
    return None


def _purge_dlq_for(webhook_id: int) -> None:
    """Clear DLQ rows owned by a given subscription so test order
    doesn't leak state through the shared in-memory DB."""
    try:
        with db.conn() as c:
            c.execute(
                "DELETE FROM webhook_dead_letter WHERE subscription_id = ?",
                (webhook_id,),
            )
    except Exception:
        pass


class _FakeResp:
    def __init__(self, status: int):
        self.status_code = status


def _client_returning(status: int):
    """Return an httpx.AsyncClient-shaped fake that yields *status*."""
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp(status)
    return _C


def _client_always_timeout():
    class _C:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise webhooks.httpx.TimeoutException("boom")
    return _C


# ── CRUD ───────────────────────────────────────────────────────────────


class TestDbCRUD(unittest.TestCase):
    def setUp(self):
        self.uid = _mk_user(f"wh_{id(self)}@t.com")

    def test_create_and_list(self):
        wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://example.com/hook",
            events=["best_bet.new", "market.resolved"],
            secret="s3cret",
        )
        rows = db.list_webhooks_for_user(self.uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], wid)
        self.assertEqual(set(json.loads(rows[0]["events"])),
                         {"best_bet.new", "market.resolved"})

    def test_list_active_filters_by_event_and_active(self):
        """Test filters by our own ids — the shared in-memory DB persists
        between tests in the same process."""
        active_id = db.create_webhook_subscription(
            user_id=self.uid, url="https://e1.example.com/h",
            events=["best_bet.new"], secret="x",
        )
        inactive_id = db.create_webhook_subscription(
            user_id=self.uid, url="https://e2.example.com/h",
            events=["market.resolved"], secret="y",
        )
        db.deactivate_webhook(inactive_id)

        bb_ids = {r["id"] for r in db.list_active_webhooks_for_event("best_bet.new")}
        self.assertIn(active_id, bb_ids)

        mr_ids = {r["id"] for r in db.list_active_webhooks_for_event("market.resolved")}
        self.assertNotIn(inactive_id, mr_ids)

    def test_delete_gates_on_owner(self):
        wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://e.com/h",
            events=["best_bet.new"], secret="s",
        )
        other = _mk_user(f"other_{id(self)}@t.com")
        self.assertFalse(db.delete_webhook_subscription(wid, other))
        self.assertTrue(db.delete_webhook_subscription(wid, self.uid))


# ── Signing + anti-replay ──────────────────────────────────────────────


class TestSignatureAndReplay(unittest.TestCase):
    def test_signature_matches_hmac_with_timestamp(self):
        """Production signature signs '<ts>.<body>' so the anti-replay header
        is itself authenticated."""
        secret = secrets.token_urlsafe(32)
        body = b'{"event":"best_bet.new","data":{"n":1}}'
        ts = 1700000000
        expected_hex = hmac.new(
            secret.encode(), b"1700000000." + body, hashlib.sha256
        ).hexdigest()
        actual_hex = webhooks._sign(secret, body, timestamp=ts)
        self.assertTrue(hmac.compare_digest(expected_hex, actual_hex))

    def test_signature_round_trips_through_verify(self):
        """Produce a real signed body and confirm the verifier accepts it."""
        secret = "shh"
        body = b'{"hello":"world"}'
        ts = int(time.time())
        sig = "sha256=" + webhooks._sign(secret, body, timestamp=ts)
        ok = webhooks.verify_signature(
            secret=secret, body=body,
            signature_header=sig, timestamp_header=str(ts),
            now=ts,
        )
        self.assertTrue(ok)

    def test_stale_timestamp_rejected(self):
        """A timestamp older than REPLAY_WINDOW_S must fail verification
        even with an otherwise-valid signature — the replay window is the
        primary defense against captured-and-resent attacks."""
        secret = "shh"
        body = b'{"x":1}'
        ts = 1700000000
        sig = "sha256=" + webhooks._sign(secret, body, timestamp=ts)
        # 'now' is 10 minutes after the timestamp — outside the 5-minute window.
        ok = webhooks.verify_signature(
            secret=secret, body=body,
            signature_header=sig, timestamp_header=str(ts),
            now=ts + 600,
        )
        self.assertFalse(ok, "stale timestamp must be rejected")

    def test_tampered_body_rejected(self):
        secret = "shh"
        ts = int(time.time())
        sig = "sha256=" + webhooks._sign(secret, b'{"n":1}', timestamp=ts)
        ok = webhooks.verify_signature(
            secret=secret, body=b'{"n":2}',   # body changed after signing
            signature_header=sig, timestamp_header=str(ts),
            now=ts,
        )
        self.assertFalse(ok)


# ── _deliver_once happy path + header content ──────────────────────────


class TestDeliverOnce(unittest.TestCase):
    def setUp(self):
        self.uid = _mk_user(f"delv_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://hooks.example.com/cb",
            events=["best_bet.new"], secret="my-secret",
        )

    def _capture_client(self, captured):
        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, content, headers):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = dict(headers)
                return _FakeResp(200)
        return FakeClient

    def test_200_no_retry_and_signed_correctly(self):
        captured: dict = {}
        with mock.patch.object(webhooks.httpx, "AsyncClient",
                               self._capture_client(captured)):
            status, err = asyncio.run(
                webhooks._deliver_once(
                    webhook_id=self.wid, url="https://hooks.example.com/cb",
                    secret="my-secret", event_type="best_bet.new",
                    body_bytes=b'{"hello":"world"}', attempt=1,
                    timestamp=1700000000,
                )
            )
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(captured["content"], b'{"hello":"world"}')
        # Signature must cover '<ts>.<body>'.
        expected = "sha256=" + hmac.new(
            b"my-secret", b"1700000000." + b'{"hello":"world"}',
            hashlib.sha256
        ).hexdigest()
        self.assertEqual(captured["headers"]["X-Narve-Signature"], expected)
        self.assertEqual(captured["headers"]["X-Narve-Event"], "best_bet.new")
        self.assertEqual(captured["headers"]["X-Narve-Timestamp"], "1700000000")


# ── Retry policy + DLQ ─────────────────────────────────────────────────


class TestRetryPolicy(unittest.TestCase):
    """Three attempts with 2s/4s/8s — verified by counting POSTs."""

    def setUp(self):
        self.uid = _mk_user(f"retry_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://example.com/h",
            events=["best_bet.new"], secret="s",
        )
        _purge_dlq_for(self.wid)

    def test_200_first_attempt_no_retry(self):
        """A 2xx on attempt 1 must short-circuit further attempts."""
        post_count = {"n": 0}

        class OneShotClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                post_count["n"] += 1
                return _FakeResp(200)

        with mock.patch.object(webhooks.httpx, "AsyncClient", OneShotClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            ok = asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))

        self.assertTrue(ok)
        self.assertEqual(post_count["n"], 1, "200 must not trigger retry")
        # No DLQ row written.
        dlq = db.list_webhook_dead_letter(limit=50)
        self.assertFalse(
            any(r["subscription_id"] == self.wid for r in dlq),
            "successful delivery must not write a DLQ row",
        )

    def test_503_retried_three_times_then_dlq(self):
        """A 5xx must trigger exactly MAX_ATTEMPTS (=3) attempts, then
        land in the DLQ."""
        post_count = {"n": 0}

        class ServerErrClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                post_count["n"] += 1
                return _FakeResp(503)

        with mock.patch.object(webhooks.httpx, "AsyncClient", ServerErrClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            ok = asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))

        self.assertFalse(ok)
        self.assertEqual(post_count["n"], webhooks.MAX_ATTEMPTS,
                         f"expected exactly {webhooks.MAX_ATTEMPTS} attempts on 5xx")
        # DLQ row was created.
        dlq = [r for r in db.list_webhook_dead_letter(limit=50)
               if r["subscription_id"] == self.wid]
        self.assertEqual(len(dlq), 1, "503 retried 3x must yield one DLQ row")
        self.assertEqual(dlq[0]["attempts"], webhooks.MAX_ATTEMPTS)

    def test_404_immediate_dlq_no_retry(self):
        """4xx must not retry — straight to DLQ on attempt 1."""
        post_count = {"n": 0}

        class NotFoundClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                post_count["n"] += 1
                return _FakeResp(404)

        with mock.patch.object(webhooks.httpx, "AsyncClient", NotFoundClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            ok = asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))

        self.assertFalse(ok)
        self.assertEqual(post_count["n"], 1,
                         "4xx must not trigger retry — user URL is broken")
        dlq = [r for r in db.list_webhook_dead_letter(limit=50)
               if r["subscription_id"] == self.wid]
        self.assertEqual(len(dlq), 1)
        self.assertEqual(dlq[0]["attempts"], 1)

    def test_timeout_is_retried(self):
        """Connection/timeout (status=None, error set) must retry — only 4xx
        is terminal."""
        post_count = {"n": 0}

        class TOClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                post_count["n"] += 1
                raise webhooks.httpx.TimeoutException("boom")

        with mock.patch.object(webhooks.httpx, "AsyncClient", TOClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))

        self.assertEqual(post_count["n"], webhooks.MAX_ATTEMPTS)


# ── Circuit breaker ────────────────────────────────────────────────────


class TestCircuitBreaker(unittest.TestCase):
    def setUp(self):
        self.uid = _mk_user(f"breaker_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://failing.example.com/h",
            events=["best_bet.new"], secret="s",
        )
        _purge_dlq_for(self.wid)

    def test_ten_consecutive_failures_open_breaker_one_hour(self):
        """After CIRCUIT_BREAKER_THRESHOLD exhausted runs, disabled_until
        must be ~now + 1h (the cooldown). The subscription stays
        is_active=1 — the auto-healing path requires that, since
        is_active=0 means user disabled it manually."""
        email_sent = []

        def _fake_email(webhook_id, consecutive):
            email_sent.append((webhook_id, consecutive))

        with mock.patch.object(webhooks.httpx, "AsyncClient",
                               _client_always_timeout()), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep), \
             mock.patch.object(webhooks, "_enqueue_disabled_email", _fake_email):
            for _ in range(webhooks.CIRCUIT_BREAKER_THRESHOLD):
                asyncio.run(webhooks._deliver_with_retries(
                    webhook_id=self.wid, url="https://failing.example.com/h",
                    secret="s", event_type="best_bet.new",
                    payload={"n": 1},
                ))

        row = db.get_webhook_subscription(self.wid)
        self.assertGreaterEqual(row["consecutive_failures"],
                                webhooks.CIRCUIT_BREAKER_THRESHOLD)
        self.assertIsNotNone(row["disabled_until"], "circuit must be open")

        now = int(time.time())
        # Cooldown window must be ~1h ahead. Allow 60s slack for slow CI.
        self.assertGreater(row["disabled_until"], now + webhooks.CIRCUIT_BREAKER_COOLDOWN_S - 60)
        self.assertLess(row["disabled_until"], now + webhooks.CIRCUIT_BREAKER_COOLDOWN_S + 60)

        self.assertTrue(email_sent, "owner must be emailed when breaker opens")

    def test_broadcast_skips_subscription_with_open_breaker(self):
        """broadcast_event must filter out subs whose breaker is open —
        a flapping subscriber doesn't get hammered for the full hour.

        The shared in-memory DB has subs from other test classes that
        listen for best_bet.new, so we can't assert a zero dispatched
        count globally. Instead we assert our specific URL was never
        POSTed during this call.
        """
        db.open_webhook_circuit(self.wid, int(time.time()) + 600)
        # Verify the breaker is open in the row.
        row = db.get_webhook_subscription(self.wid)
        self.assertTrue(webhooks._circuit_open(row))

        urls_posted: list = []

        class CountClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, content, headers):
                urls_posted.append(url)
                return _FakeResp(200)

        with mock.patch.object(webhooks.httpx, "AsyncClient", CountClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            asyncio.run(webhooks.broadcast_event("best_bet.new", {"x": 1}))

        self.assertNotIn(
            "https://failing.example.com/h", urls_posted,
            "open-breaker sub URL must not be POSTed",
        )

    def test_success_after_cooldown_closes_breaker(self):
        """A 2xx delivery must reset consecutive_failures AND clear
        disabled_until — the auto-heal contract."""
        # Manually open the breaker (simulate having gotten there).
        db.open_webhook_circuit(self.wid, int(time.time()) + 600)
        db.bump_webhook_failure(self.wid)  # also bump the counter

        class OkClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return _FakeResp(200)

        with mock.patch.object(webhooks.httpx, "AsyncClient", OkClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            ok = asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://failing.example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))

        self.assertTrue(ok)
        row = db.get_webhook_subscription(self.wid)
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNone(row["disabled_until"], "breaker must close on success")


# ── DLQ replay ─────────────────────────────────────────────────────────


class TestDlqReplay(unittest.TestCase):
    def setUp(self):
        self.uid = _mk_user(f"dlq_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://ok.example.com/h",
            events=["best_bet.new"], secret="s",
        )

    def test_replay_marks_row_requeued(self):
        dlq_id = db.record_webhook_dead_letter(
            subscription_id=self.wid, event_type="best_bet.new",
            payload='{"event":"best_bet.new","data":{}}',
            last_error="http 503", attempts=3,
            first_failed_at=int(time.time()) - 100,
        )

        class OkClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return _FakeResp(200)

        with mock.patch.object(webhooks.httpx, "AsyncClient", OkClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            result = asyncio.run(webhooks.replay_dead_letter(dlq_id))

        self.assertTrue(result["ok"])
        row = db.get_webhook_dead_letter(dlq_id)
        self.assertIsNotNone(row["requeued_at"])


if __name__ == "__main__":
    unittest.main()

"""Tests for the outbound webhooks subsystem.

Covers:
  - db CRUD: create / list_for_user / get / deactivate / delete
  - list_active_webhooks_for_event filters by event AND by is_active
  - HMAC signature matches hmac.new(secret, body) → sha256
  - _deliver_once happy path (via mocked httpx.AsyncClient)
  - retry budget: 5 consecutive failures marks the sub inactive +
    invokes the disabled-email hook
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import unittest
from unittest import mock

from tests import _testdb  # noqa: F401

import db
import webhooks


def _mk_user(email: str) -> int:
    return db.create_user(email, "pw-" * 4, username=email.split("@")[0])


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
        # events stored as JSON array
        self.assertEqual(set(json.loads(rows[0]["events"])),
                         {"best_bet.new", "market.resolved"})

    def test_list_active_filters_by_event_and_active(self):
        """Test filters by *our own* ids rather than absolute row counts —
        the shared in-memory DB in tests/_testdb.py persists between tests
        in the same process, so other tests' subscriptions are visible."""
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
        self.assertIn(active_id, bb_ids,
                      "our active best_bet.new subscription should match")

        mr_ids = {r["id"] for r in db.list_active_webhooks_for_event("market.resolved")}
        self.assertNotIn(inactive_id, mr_ids,
                         "deactivated subscription must not be in the active set")

    def test_delete_gates_on_owner(self):
        wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://e.com/h",
            events=["best_bet.new"], secret="s",
        )
        other = _mk_user(f"other_{id(self)}@t.com")
        self.assertFalse(db.delete_webhook_subscription(wid, other))
        self.assertTrue(db.delete_webhook_subscription(wid, self.uid))


class TestSignature(unittest.TestCase):
    def test_signature_matches_hmac(self):
        secret = secrets.token_urlsafe(32)
        body = b'{"event":"best_bet.new","data":{"n":1}}'
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # Directly call the internal signer — also exercised end-to-end below
        # in test_deliver_once_happy_path.
        actual = "sha256=" + webhooks._sign(secret, body)
        self.assertTrue(hmac.compare_digest(expected, actual))


class TestDeliverOnce(unittest.TestCase):
    """Unit test the single-attempt path with a mocked httpx.AsyncClient."""

    def setUp(self):
        self.uid = _mk_user(f"delv_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://hooks.example.com/cb",
            events=["best_bet.new"], secret="my-secret",
        )

    def _capture_client(self, captured):
        class FakeResp:
            def __init__(self, status): self.status_code = status

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, content, headers):
                captured["url"] = url
                captured["content"] = content
                captured["headers"] = dict(headers)
                return FakeResp(200)
        return FakeClient

    def test_happy_path_returns_200_and_signs_body(self):
        captured: dict = {}
        with mock.patch.object(webhooks.httpx, "AsyncClient",
                               self._capture_client(captured)):
            status, err = asyncio.run(
                webhooks._deliver_once(
                    webhook_id=self.wid, url="https://hooks.example.com/cb",
                    secret="my-secret", event_type="best_bet.new",
                    body_bytes=b'{"hello":"world"}', attempt=1,
                )
            )
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        # Body was posted unchanged
        self.assertEqual(captured["content"], b'{"hello":"world"}')
        # Signature header computed from (secret, body)
        expected = "sha256=" + hmac.new(b"my-secret", b'{"hello":"world"}', hashlib.sha256).hexdigest()
        self.assertEqual(captured["headers"]["X-Narve-Signature"], expected)
        self.assertEqual(captured["headers"]["X-Narve-Event"], "best_bet.new")


class TestRetryBudget(unittest.TestCase):
    """Exercises _deliver_with_retries with zero sleeps + a mocked httpx."""

    def setUp(self):
        self.uid = _mk_user(f"retry_{id(self)}@t.com")
        self.wid = db.create_webhook_subscription(
            user_id=self.uid, url="https://failing.example.com/h",
            events=["best_bet.new"], secret="s",
        )

    def test_five_failures_disables_and_emails(self):
        # Make httpx always error out.
        class FailingClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                raise webhooks.httpx.TimeoutException("boom")

        # No-op sleep so the test runs instantly.
        async def _no_sleep(_):
            return None

        email_sent = []
        def _fake_email(webhook_id, consecutive):
            email_sent.append((webhook_id, consecutive))

        with mock.patch.object(webhooks.httpx, "AsyncClient", FailingClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep), \
             mock.patch.object(webhooks, "_enqueue_disabled_email", _fake_email):
            ok = asyncio.run(
                webhooks._deliver_with_retries(
                    webhook_id=self.wid, url="https://failing.example.com/h",
                    secret="s", event_type="best_bet.new",
                    payload={"n": 1},
                )
            )

        self.assertFalse(ok)
        # consecutive_failures incremented exactly once per exhausted run
        row = db.get_webhook_subscription(self.wid)
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertEqual(row["is_active"], 1)  # not yet disabled

        # Four more exhausted runs → 5 consecutive → disable + email
        for _ in range(4):
            with mock.patch.object(webhooks.httpx, "AsyncClient", FailingClient), \
                 mock.patch.object(webhooks.asyncio, "sleep", _no_sleep), \
                 mock.patch.object(webhooks, "_enqueue_disabled_email", _fake_email):
                asyncio.run(webhooks._deliver_with_retries(
                    webhook_id=self.wid, url="https://failing.example.com/h",
                    secret="s", event_type="best_bet.new",
                    payload={"n": 1},
                ))

        row = db.get_webhook_subscription(self.wid)
        self.assertGreaterEqual(row["consecutive_failures"], 5)
        self.assertEqual(row["is_active"], 0)   # auto-disabled
        self.assertTrue(email_sent, "expected _enqueue_disabled_email to fire at least once")

    def test_success_resets_consecutive(self):
        # Seed a failure, then a success, and confirm counter resets.
        db.bump_webhook_failure(self.wid)
        self.assertEqual(db.get_webhook_subscription(self.wid)["consecutive_failures"], 1)

        class OkClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw):
                class R: status_code = 200
                return R()

        async def _no_sleep(_): return None

        with mock.patch.object(webhooks.httpx, "AsyncClient", OkClient), \
             mock.patch.object(webhooks.asyncio, "sleep", _no_sleep):
            ok = asyncio.run(webhooks._deliver_with_retries(
                webhook_id=self.wid, url="https://ok.example.com/h",
                secret="s", event_type="best_bet.new",
                payload={"n": 1},
            ))
        self.assertTrue(ok)
        row = db.get_webhook_subscription(self.wid)
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNotNone(row["last_delivered_at"])


if __name__ == "__main__":
    unittest.main()

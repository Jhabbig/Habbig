"""Stripe webhook hardening tests (idempotency + mode check).

The helpers don't hit real Stripe — they only need a DB + event dicts.
We ingest two events with the same id and verify the second one short-
circuits as ``already_processed``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        # Every module that does a top-level ``import db`` caches the
        # *old* DB path via closure. Reload them so their module-level
        # ``import db`` picks up the new GATEWAY_DB_PATH.
        for mod in ("db", "migrations", "stripe_webhook_hardening"):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        os.environ.pop("PRODUCTION", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _event(self, **over):
        base = {
            "id": "evt_test_1",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": {"metadata": {}}},
        }
        base.update(over)
        return base

    def test_first_event_not_already_processed(self):
        from stripe_webhook_hardening import mark_received
        resp = mark_received(self._event())
        self.assertIsNone(resp, "first ingest must not short-circuit")

    def test_second_event_already_processed(self):
        from stripe_webhook_hardening import mark_received
        evt = self._event()
        self.assertIsNone(mark_received(evt))
        resp = mark_received(evt)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 200)
        # Body is JSON {"status": "already_processed"}
        import json
        body = json.loads(bytes(resp.body))
        self.assertEqual(body.get("status"), "already_processed")

    def test_mode_mismatch_in_production(self):
        os.environ["PRODUCTION"] = "1"
        from stripe_webhook_hardening import reject_mode_mismatch
        # Production but event.livemode=False → reject.
        resp = reject_mode_mismatch(self._event(livemode=False))
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 400)

    def test_mode_match_in_production(self):
        os.environ["PRODUCTION"] = "1"
        from stripe_webhook_hardening import reject_mode_mismatch
        resp = reject_mode_mismatch(self._event(livemode=True))
        self.assertIsNone(resp)

    def test_mark_processed_stamps_row(self):
        from stripe_webhook_hardening import mark_processed, mark_received
        evt = self._event()
        mark_received(evt)
        mark_processed(evt)
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT processed_at, error FROM processed_stripe_events "
                "WHERE event_id = ?",
                (evt["id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["processed_at"])
        self.assertIsNone(row["error"])

    def test_mark_processed_records_error(self):
        from stripe_webhook_hardening import mark_processed, mark_received
        evt = self._event(id="evt_test_error")
        mark_received(evt)
        mark_processed(evt, error="boom")
        import db
        with db.conn() as c:
            row = c.execute(
                "SELECT error FROM processed_stripe_events WHERE event_id = ?",
                (evt["id"],),
            ).fetchone()
        self.assertEqual(row["error"], "boom")

    def test_mark_received_handles_missing_event_id(self):
        from stripe_webhook_hardening import mark_received
        # No id → handler should not crash and should return None so the
        # webhook handler can decide how to log the malformed shape.
        resp = mark_received({"type": "x", "livemode": False})
        self.assertIsNone(resp)

    def test_mark_processed_no_id_is_noop(self):
        from stripe_webhook_hardening import mark_processed
        # Should not raise even though there's no ledger row to update.
        mark_processed({"type": "x"})

    def test_mode_match_in_test_env(self):
        # PRODUCTION unset (default 0) and livemode=False → match, no reject.
        from stripe_webhook_hardening import reject_mode_mismatch
        resp = reject_mode_mismatch(self._event(livemode=False))
        self.assertIsNone(resp)


def _seed_user(email: str, username: str) -> int:
    """Insert a user row directly via raw SQL on the freshly-loaded
    `db` module so we hit the tempfile GATEWAY_DB_PATH connection.

    We can't call `db.create_user` here — that re-exports through
    ``queries.auth`` which was imported earlier with a bound reference
    to the conftest's in-memory shared conn. SQL into ``db.conn()``
    sidesteps the binding and lands in our tempfile.
    """
    import db
    import time as _time
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO users (username, email, password_hash, "
            "password_salt, created_at, is_admin) "
            "VALUES (?, ?, '', '', ?, 0)",
            (username, email, int(_time.time())),
        )
        return int(cur.lastrowid)


class TestUserIdResolution(unittest.TestCase):
    """Cover `_user_id_from_event` — both the metadata path and the
    customer-lookup fallback (which silently fails when the column doesn't
    exist on this schema revision)."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        for mod in ("db", "migrations", "stripe_webhook_hardening"):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()
        self.uid = _seed_user("uid_test@test.example", "uidtest")

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _evt(self, obj):
        return {
            "id": "evt_uid",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": obj},
        }

    def test_user_id_from_metadata(self):
        from stripe_webhook_hardening import _user_id_from_event
        evt = self._evt({"metadata": {"user_id": str(self.uid)}})
        self.assertEqual(_user_id_from_event(evt), self.uid)

    def test_user_id_from_narve_user_id_alias(self):
        from stripe_webhook_hardening import _user_id_from_event
        evt = self._evt({"metadata": {"narve_user_id": str(self.uid)}})
        self.assertEqual(_user_id_from_event(evt), self.uid)

    def test_user_id_invalid_value_returns_none(self):
        from stripe_webhook_hardening import _user_id_from_event
        evt = self._evt({"metadata": {"user_id": "not_an_int"}})
        # Bad value falls through to the customer-lookup branch (no
        # customer field), then returns None.
        self.assertIsNone(_user_id_from_event(evt))

    def test_user_id_missing_returns_none(self):
        from stripe_webhook_hardening import _user_id_from_event
        evt = self._evt({"metadata": {}})
        self.assertIsNone(_user_id_from_event(evt))

    def test_user_id_customer_fallback_swallows_missing_column(self):
        # `users.stripe_customer_id` is not a column on this schema, so
        # the customer-lookup branch raises and is swallowed. The helper
        # must return None, not propagate.
        from stripe_webhook_hardening import _user_id_from_event
        evt = self._evt({"metadata": {}, "customer": "cus_xyz"})
        self.assertIsNone(_user_id_from_event(evt))


class TestApplySubscriptionCancelled(unittest.TestCase):
    """Cover `apply_subscription_cancelled` — the money path. Sessions get
    revoked, embed widgets deactivated, subproduct status flipped to
    'canceled', and a cancellation email enqueued."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        # Note: don't reload jobs.email_jobs — its module-top
        # `@register_job("send_email")` raises ValueError on re-import.
        # stripe_webhook_hardening imports it lazily so a fresh
        # stripe_webhook_hardening picks up the already-loaded module.
        for mod in (
            "db", "migrations", "stripe_webhook_hardening",
            "subproduct_access",
        ):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()
        self.uid = _seed_user("cancel@test.example", "canceluser")
        # Seed an active subproduct_subscription so the cancellation
        # update has something to flip.
        import json
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
                (json.dumps({"climate": {"status": "active"}}), self.uid),
            )

        # Patch enqueue_email so we don't need a running asyncio loop or
        # the full job backend. Capture calls so we can assert on them.
        import jobs.email_jobs as email_jobs

        self._enqueue_calls = []

        async def _fake_enqueue_email(**kw):
            self._enqueue_calls.append(kw)
            return 1

        self._orig_enqueue = email_jobs.enqueue_email
        email_jobs.enqueue_email = _fake_enqueue_email  # type: ignore[assignment]

        # stripe_webhook_hardening tries `asyncio.get_event_loop()
        # .create_task(coro)` first and falls back to `asyncio.run(coro)`
        # if that raises. Under pytest there's an idle loop, so
        # create_task succeeds but the coroutine never gets awaited —
        # our fake never fires. Force the RuntimeError fallback so
        # `asyncio.run(coro)` executes the fake synchronously.
        import asyncio as _asyncio
        self._orig_get_event_loop = _asyncio.get_event_loop

        def _no_loop():
            raise RuntimeError("no running loop (forced for test)")

        _asyncio.get_event_loop = _no_loop  # type: ignore[assignment]

        # `stripe_webhook_hardening` calls `db.get_user_by_id(user_id)`
        # to look up the email. That helper is `queries.auth.get_user_by_id`
        # bound to the original conftest-patched `db.conn` — which is a
        # different in-memory connection from our tempfile. Stub it.
        import db as _db
        self._orig_get_user_by_id = _db.get_user_by_id
        _email = "cancel@test.example"
        _uid = self.uid
        class _Row:
            def __init__(self):
                self._d = {"id": _uid, "email": _email}
            def __getitem__(self, k):
                return self._d[k]
            def keys(self):
                return self._d.keys()
        _db.get_user_by_id = lambda uid: _Row() if uid == _uid else None  # type: ignore[assignment]

    def tearDown(self):
        import jobs.email_jobs as email_jobs
        email_jobs.enqueue_email = self._orig_enqueue  # type: ignore[assignment]
        import db as _db
        _db.get_user_by_id = self._orig_get_user_by_id  # type: ignore[assignment]
        import asyncio as _asyncio
        _asyncio.get_event_loop = self._orig_get_event_loop  # type: ignore[assignment]
        os.environ.pop("GATEWAY_DB_PATH", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _cancel_event(self, **obj_over):
        obj = {
            "metadata": {
                "user_id": str(self.uid),
                "subproduct_slug": "climate",
            },
            "cancel_at": 1737000000,
        }
        obj.update(obj_over)
        return {
            "id": "evt_cancel_1",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": obj},
        }

    def test_cancel_flips_subproduct_status(self):
        from stripe_webhook_hardening import apply_subscription_cancelled
        apply_subscription_cancelled(self._cancel_event())
        import db, json as _json
        with db.conn() as c:
            row = c.execute(
                "SELECT subproduct_subscriptions FROM users WHERE id = ?",
                (self.uid,),
            ).fetchone()
        blob = _json.loads(row["subproduct_subscriptions"])
        self.assertEqual(blob["climate"]["status"], "canceled")

    def test_cancel_enqueues_email_with_period_end_date(self):
        from stripe_webhook_hardening import apply_subscription_cancelled
        apply_subscription_cancelled(self._cancel_event(cancel_at=1737000000))
        self.assertEqual(len(self._enqueue_calls), 1)
        call = self._enqueue_calls[0]
        self.assertEqual(call["template"], "subscription_cancelled")
        self.assertEqual(call["to"], "cancel@test.example")
        self.assertEqual(call["context"]["user_id"], self.uid)
        self.assertEqual(call["context"]["subproduct_slug"], "climate")
        # 1737000000 → 2025-01-16 in UTC; assert non-empty ISO date.
        self.assertTrue(call["context"]["period_end_date"])
        self.assertIn("-", call["context"]["period_end_date"])

    def test_cancel_without_user_id_does_not_email(self):
        from stripe_webhook_hardening import apply_subscription_cancelled
        evt = {
            "id": "evt_cancel_noid",
            "type": "customer.subscription.deleted",
            "livemode": False,
            "data": {"object": {"metadata": {}}},
        }
        apply_subscription_cancelled(evt)
        self.assertEqual(self._enqueue_calls, [])

    def test_cancel_without_slug_still_emails(self):
        # No slug → status update is skipped but the email still fires
        # so the customer learns their subscription ended.
        from stripe_webhook_hardening import apply_subscription_cancelled
        evt = self._cancel_event(metadata={"user_id": str(self.uid)})
        apply_subscription_cancelled(evt)
        self.assertEqual(len(self._enqueue_calls), 1)
        self.assertEqual(
            self._enqueue_calls[0]["context"]["subproduct_slug"], ""
        )

    def test_cancel_invalid_timestamp_is_safe(self):
        from stripe_webhook_hardening import apply_subscription_cancelled
        evt = self._cancel_event(cancel_at="not_a_timestamp")
        apply_subscription_cancelled(evt)
        # Email still queued, period_end_date falls back to empty string.
        self.assertEqual(len(self._enqueue_calls), 1)
        self.assertEqual(
            self._enqueue_calls[0]["context"]["period_end_date"], ""
        )


class TestApplyInvoicePaymentFailed(unittest.TestCase):
    """Cover `apply_invoice_payment_failed` — flips status to past_due."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        for mod in (
            "db", "migrations", "stripe_webhook_hardening",
            "subproduct_access",
        ):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()
        self.uid = _seed_user("pf@test.example", "pfuser")
        import json
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
                (json.dumps({"markets": {"status": "active"}}), self.uid),
            )

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        os.environ.pop("STRIPE_SECRET_KEY", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_no_user_id_is_safe(self):
        from stripe_webhook_hardening import apply_invoice_payment_failed
        # No metadata, no customer, no subscription id — nothing happens
        # and nothing raises.
        apply_invoice_payment_failed({
            "id": "evt_pf_nouser",
            "type": "invoice.payment_failed",
            "livemode": False,
            "data": {"object": {}},
        })

    def test_no_stripe_key_skips_slug_lookup(self):
        # STRIPE_SECRET_KEY missing → `_lookup_subproduct_slug` returns
        # None → `apply_invoice_payment_failed` cannot find a slug and
        # leaves the subproduct row alone. No crash.
        os.environ.pop("STRIPE_SECRET_KEY", None)
        from stripe_webhook_hardening import apply_invoice_payment_failed
        apply_invoice_payment_failed({
            "id": "evt_pf",
            "type": "invoice.payment_failed",
            "livemode": False,
            "data": {"object": {
                "metadata": {"user_id": str(self.uid)},
                "subscription": "sub_xyz",
            }},
        })
        # Status unchanged because we never resolved the slug.
        import db, json as _json
        with db.conn() as c:
            row = c.execute(
                "SELECT subproduct_subscriptions FROM users WHERE id = ?",
                (self.uid,),
            ).fetchone()
        blob = _json.loads(row["subproduct_subscriptions"])
        self.assertEqual(blob["markets"]["status"], "active")


class TestUpdateSubproductStatus(unittest.TestCase):
    """Cover `_update_subproduct_status` — the JSON-blob mutator that
    powers both the cancel and past_due paths."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        os.environ["GATEWAY_DB_PATH"] = self._tmp.name
        for mod in ("db", "migrations", "stripe_webhook_hardening"):
            if mod in sys.modules:
                del sys.modules[mod]
        import db
        db.init_db()
        import migrations
        migrations.upgrade_to_head()
        self.uid = _seed_user("blob@test.example", "blobuser")

    def tearDown(self):
        os.environ.pop("GATEWAY_DB_PATH", None)
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_update_adds_status_to_new_slug(self):
        from stripe_webhook_hardening import _update_subproduct_status
        _update_subproduct_status(self.uid, "elections", "canceled")
        import db, json as _json
        with db.conn() as c:
            row = c.execute(
                "SELECT subproduct_subscriptions FROM users WHERE id = ?",
                (self.uid,),
            ).fetchone()
        blob = _json.loads(row["subproduct_subscriptions"])
        self.assertEqual(blob["elections"]["status"], "canceled")

    def test_update_overwrites_existing_status(self):
        import db, json
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
                (json.dumps({"markets": {"status": "active"}}), self.uid),
            )
        from stripe_webhook_hardening import _update_subproduct_status
        _update_subproduct_status(self.uid, "markets", "past_due")
        with db.conn() as c:
            row = c.execute(
                "SELECT subproduct_subscriptions FROM users WHERE id = ?",
                (self.uid,),
            ).fetchone()
        blob = json.loads(row["subproduct_subscriptions"])
        self.assertEqual(blob["markets"]["status"], "past_due")

    def test_update_corrupt_blob_recovers(self):
        # If the JSON column is non-JSON garbage, the helper resets to
        # an empty dict rather than blowing up.
        import db, json as _json
        with db.conn() as c:
            c.execute(
                "UPDATE users SET subproduct_subscriptions = ? WHERE id = ?",
                ("not valid json {{{", self.uid),
            )
        from stripe_webhook_hardening import _update_subproduct_status
        _update_subproduct_status(self.uid, "climate", "canceled")
        with db.conn() as c:
            row = c.execute(
                "SELECT subproduct_subscriptions FROM users WHERE id = ?",
                (self.uid,),
            ).fetchone()
        blob = _json.loads(row["subproduct_subscriptions"])
        self.assertEqual(blob, {"climate": {"status": "canceled"}})

    def test_update_unknown_user_is_noop(self):
        from stripe_webhook_hardening import _update_subproduct_status
        # Should not raise even when the user_id doesn't exist.
        _update_subproduct_status(999_999, "markets", "canceled")


class TestLookupSubproductSlug(unittest.TestCase):
    """`_lookup_subproduct_slug` returns None when there's no Stripe key,
    so we can cover the early-exit branch without mocking Stripe."""

    def tearDown(self):
        os.environ.pop("STRIPE_SECRET_KEY", None)

    def test_no_api_key_returns_none(self):
        os.environ.pop("STRIPE_SECRET_KEY", None)
        for mod in ("stripe_webhook_hardening",):
            if mod in sys.modules:
                del sys.modules[mod]
        from stripe_webhook_hardening import _lookup_subproduct_slug
        self.assertIsNone(_lookup_subproduct_slug("sub_anything"))


if __name__ == "__main__":
    unittest.main()

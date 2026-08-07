import asyncio
import json
import time

import pytest
from fastapi import HTTPException

import db
import email_relay.notify as relay_notify
import server as gw


class FakeRequest:
    def __init__(self, event: dict):
        self._body = json.dumps(event).encode()
        self.headers = {"stripe-signature": "t=1,v1=stub"}

    async def body(self) -> bytes:
        return self._body


@pytest.fixture
def stripe_env(monkeypatch):
    monkeypatch.setattr(gw, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(
        gw.stripe.Webhook,
        "construct_event",
        staticmethod(lambda payload, sig, secret: json.loads(payload)),
    )
    gw._SESSION_CACHE.clear()
    gw._SUB_CACHE.clear()


def _post(event: dict):
    return asyncio.run(gw.stripe_webhook(FakeRequest(event)))


def _body(resp) -> dict:
    return json.loads(resp.body)


def _make_stripe_sub(email: str, stripe_sub_id: str, dashboard_key: str = "crypto") -> int:
    uid = db.create_user(email, "pw-123456789")
    db.upsert_subscription(uid, dashboard_key, "monthly", duration_days=1, source="stripe", stripe_sub_id=stripe_sub_id)
    return uid


def _expires_at(uid: int) -> int:
    return db.list_subscriptions(uid)[0]["expires_at"]


def _invoice_paid_event(event_id: str, stripe_sub_id: str, period_end: int) -> dict:
    return {
        "id": event_id,
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_test",
                "subscription": stripe_sub_id,
                "lines": {"data": [{"period": {"end": period_end}}]},
            }
        },
    }


def test_invoice_paid_extends_expiry(fresh_db, stripe_env):
    uid = _make_stripe_sub("renew@example.com", "sub_renew")
    new_end = int(time.time()) + 45 * 86400
    resp = _post(_invoice_paid_event("evt_renew_1", "sub_renew", new_end))
    assert _body(resp)["status"] == "ok"
    sub = db.list_subscriptions(uid)[0]
    assert sub["expires_at"] == new_end
    assert sub["status"] == "active"
    assert db.stripe_event_already_processed("evt_renew_1")


def test_invoice_paid_reactivates_cancelled_row(fresh_db, stripe_env):
    uid = _make_stripe_sub("lapsed@example.com", "sub_lapsed")
    db.cancel_subscription_by_stripe_id("sub_lapsed")
    new_end = int(time.time()) + 30 * 86400
    _post(_invoice_paid_event("evt_renew_2", "sub_lapsed", new_end))
    assert db.has_active_subscription(uid, "crypto")


def test_subscription_updated_uses_stripe_period_end(fresh_db, stripe_env):
    uid = _make_stripe_sub("updated@example.com", "sub_upd")
    new_end = int(time.time()) + 400 * 86400
    event = {
        "id": "evt_upd_1",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_upd",
                "status": "active",
                "items": {"data": [{"current_period_end": new_end}]},
            }
        },
    }
    _post(event)
    assert _expires_at(uid) == new_end


def test_fulfillment_failure_leaves_event_unmarked_then_retry_succeeds(fresh_db, stripe_env, monkeypatch):
    uid = _make_stripe_sub("flaky@example.com", "sub_flaky")
    original_expiry = _expires_at(uid)
    new_end = int(time.time()) + 60 * 86400
    event = _invoice_paid_event("evt_flaky_1", "sub_flaky", new_end)

    real_renew = db.renew_subscription_by_stripe_id

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(db, "renew_subscription_by_stripe_id", boom)
    with pytest.raises(HTTPException) as exc_info:
        _post(event)
    assert exc_info.value.status_code == 500
    assert not db.stripe_event_already_processed("evt_flaky_1")
    assert _expires_at(uid) == original_expiry

    monkeypatch.setattr(db, "renew_subscription_by_stripe_id", real_renew)
    resp = _post(event)
    assert _body(resp)["status"] == "ok"
    assert _expires_at(uid) == new_end
    assert db.stripe_event_already_processed("evt_flaky_1")


def test_duplicate_event_id_is_noop(fresh_db, stripe_env):
    uid = _make_stripe_sub("dup@example.com", "sub_dup")
    first_end = int(time.time()) + 30 * 86400
    _post(_invoice_paid_event("evt_dup_1", "sub_dup", first_end))
    assert _expires_at(uid) == first_end

    resp = _post(_invoice_paid_event("evt_dup_1", "sub_dup", first_end + 999))
    assert _body(resp)["duplicate"] is True
    assert _expires_at(uid) == first_end


def test_checkout_completed_activates_dashboard(fresh_db, stripe_env):
    uid = db.create_user("buyer@example.com", "pw-123456789")
    key = next(iter(gw.DASHBOARDS))
    event = {
        "id": "evt_co_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "subscription": "sub_new",
                "metadata": {"type": "dashboard", "dashboard_key": key, "plan": "monthly", "user_id": str(uid)},
            }
        },
    }
    _post(event)
    assert db.has_active_subscription(uid, key)
    assert db.stripe_event_already_processed("evt_co_1")


def test_subscription_deleted_cancels(fresh_db, stripe_env):
    uid = _make_stripe_sub("gone@example.com", "sub_gone")
    event = {
        "id": "evt_del_1",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_gone"}},
    }
    _post(event)
    assert not db.has_active_subscription(uid, "crypto")
    assert db.list_subscriptions(uid)[0]["status"] == "cancelled"


def test_alert_helper_never_raises(monkeypatch):
    monkeypatch.setattr(gw, "BILLING_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.invalid")
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "pw")

    def smtp_down(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(relay_notify.smtplib, "SMTP", smtp_down)
    gw._alert_billing_failure("evt_x", "invoice.paid", RuntimeError("boom"))


def test_alert_helper_off_by_default(monkeypatch):
    monkeypatch.setattr(gw, "BILLING_ALERT_EMAIL", "")

    def explode(*args, **kwargs):
        raise AssertionError("must not attempt SMTP when unconfigured")

    monkeypatch.setattr(relay_notify.smtplib, "SMTP", explode)
    gw._alert_billing_failure("evt_y", "invoice.paid", RuntimeError("boom"))

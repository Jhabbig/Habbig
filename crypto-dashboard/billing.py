#!/usr/bin/env python3
"""
Stripe-backed subscription billing with three tiers: free, pro, wealth.

Why direct REST instead of the `stripe` Python SDK:
  - One less dependency.
  - The two endpoints we need (Checkout Sessions + webhook signature
    verification) are trivial — bearer-auth POST and HMAC-SHA256.

Env vars (all optional — billing is disabled if STRIPE_SECRET_KEY is unset):
  STRIPE_SECRET_KEY        sk_test_... or sk_live_...
  STRIPE_WEBHOOK_SECRET    whsec_... (signs webhook payloads)
  STRIPE_PRICE_PRO         price_... (Pro tier monthly price ID)
  STRIPE_PRICE_WEALTH      price_... (Wealth tier monthly price ID)
  STRIPE_SUCCESS_URL       where to redirect after checkout (default /long-term)
  STRIPE_CANCEL_URL        where to redirect on cancel (default /pricing)

When billing is disabled (no STRIPE_SECRET_KEY), every authenticated user
is treated as free — the wizard still works, dry-run still works, and
gated features show a "billing not configured" notice instead of a
checkout link. This is the dev/self-hosted mode.

Feature gating model — `require_tier(user, min_tier)` returns True if the
user's tier ≥ min_tier in the ordering free < pro < wealth < admin.
Admin always passes (set by the gateway for support staff). The map of
feature → minimum tier lives in `FEATURE_TIERS` so it's one place to
audit/adjust.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional
from urllib.parse import urlencode

import requests

import database as db

log = logging.getLogger("crypto.billing")

STRIPE_BASE = "https://api.stripe.com/v1"

TIER_ORDER = {"free": 0, "pro": 1, "wealth": 2, "admin": 99}

# Map a feature key to the minimum tier required. Keep this list narrow
# and audit-able — anything not listed is implicitly free.
FEATURE_TIERS = {
    "exchange_connect":       "pro",     # save Coinbase/Kraken creds
    "live_execution":         "pro",     # flip dry-run OFF
    "push_notifications":     "free",    # push is free — it drives engagement
    "tax_harvest_execute":    "pro",     # one-click harvest-now button
    "tax_form_8949":          "pro",     # CSV download
    "strategy_publish":       "pro",     # set visibility=public
    "extra_strategy_subs":    "wealth",  # past the first 3 subscriptions
    "multi_exchange":         "wealth",  # link both Coinbase AND Kraken
    "priority_support":       "wealth",
}

# Subscription limits (per tier). None = unlimited.
SUB_LIMITS = {"free": 1, "pro": 3, "wealth": None, "admin": None}


# ─── Tier resolution ────────────────────────────────────────────────────────

def get_tier(user_id: str, gateway_tier: str | None = None) -> str:
    """Resolve the user's effective tier.
    1. Gateway "admin" always wins (support / ops).
    2. Otherwise look at the billing row.
    3. Default to "free"."""
    if gateway_tier == "admin":
        return "admin"
    row = db.get_billing_row(user_id)
    if not row:
        return "free"
    status = (row.get("status") or "").lower()
    tier = (row.get("tier") or "free").lower()
    # If subscription lapsed (past_due / cancelled past the period end) →
    # drop them back to free without manual intervention.
    if status in ("past_due", "cancelled", "canceled", "incomplete_expired"):
        return "free"
    return tier if tier in TIER_ORDER else "free"


def require_tier(user_tier: str, min_tier: str) -> bool:
    return TIER_ORDER.get(user_tier, 0) >= TIER_ORDER.get(min_tier, 0)


def feature_allowed(user_tier: str, feature: str) -> bool:
    min_tier = FEATURE_TIERS.get(feature, "free")
    return require_tier(user_tier, min_tier)


def subscription_limit(user_tier: str) -> Optional[int]:
    return SUB_LIMITS.get(user_tier, 1)


def billing_configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


# ─── Stripe API ─────────────────────────────────────────────────────────────

def _stripe_post(path: str, data: dict) -> dict:
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return {"error": "billing not configured"}
    try:
        # Stripe accepts form-urlencoded for the v1 REST API. Nested keys go
        # in PHP-style: `line_items[0][price]=...`.
        r = requests.post(
            f"{STRIPE_BASE}{path}",
            data=_stripe_form_encode(data),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Stripe-Version": "2024-12-18.acacia",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {}).get("message", r.text[:200])
            except ValueError:
                err = r.text[:200]
            return {"error": err, "status": r.status_code}
        return r.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def _stripe_form_encode(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe expects nested dicts/lists as PHP-style form keys.
       Returns a flat list of (key, value) tuples for requests."""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out.extend(_stripe_form_encode(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.extend(_stripe_form_encode(item, f"{key}[{i}]"))
                else:
                    out.append((f"{key}[{i}]", str(item)))
        elif v is None:
            continue
        else:
            out.append((key, str(v)))
    return out


def create_checkout_session(user_id: str, email: str, tier: str,
                            success_url: str, cancel_url: str) -> dict:
    """Create a Stripe Checkout Session for the given tier. Returns the
    session URL the user should redirect to."""
    if tier not in ("pro", "wealth"):
        return {"error": "tier must be pro or wealth"}
    price_id = os.environ.get(
        "STRIPE_PRICE_PRO" if tier == "pro" else "STRIPE_PRICE_WEALTH", "",
    ).strip()
    if not price_id:
        return {"error": f"price id not configured for tier {tier}"}
    # Look up or pre-fill the customer id so they can manage the
    # subscription in the Stripe Customer Portal later.
    existing = db.get_billing_row(user_id)
    customer = (existing or {}).get("stripe_customer_id") or None
    data: dict = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": user_id,
        # Echo metadata back via the webhook so we can map session →
        # user_id even if customer hasn't been created yet.
        "metadata": {"narve_user_id": user_id, "narve_tier": tier},
        "allow_promotion_codes": "true",
    }
    if customer:
        data["customer"] = customer
    else:
        data["customer_email"] = email
    return _stripe_post("/checkout/sessions", data)


def create_billing_portal_session(user_id: str, return_url: str) -> dict:
    """Customer Portal lets the user upgrade/downgrade/cancel without
    re-onboarding through Checkout."""
    row = db.get_billing_row(user_id)
    customer = (row or {}).get("stripe_customer_id")
    if not customer:
        return {"error": "no Stripe customer on file — start with Checkout first"}
    return _stripe_post("/billing_portal/sessions", {
        "customer": customer, "return_url": return_url,
    })


# ─── Webhook ────────────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, sig_header: str,
                              tolerance: int = 300) -> bool:
    """Verify Stripe's `Stripe-Signature` header.
    Header format: `t=<unix_ts>,v1=<hmac_sha256_hex>[,v0=...]`.
    We HMAC-SHA256(`<ts>.<body>`) with the webhook secret and compare in
    constant time. The tolerance is the max acceptable age of the signature
    (default 5 min, matching Stripe's docs)."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        return False
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts = parts.get("t")
    v1 = parts.get("v1")
    if not ts or not v1:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > tolerance:
        return False
    signed = f"{ts}.{payload.decode('utf-8', 'replace')}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def handle_webhook_event(event: dict) -> dict:
    """Apply a verified Stripe webhook event to our billing table.
    Idempotent — Stripe retries failed deliveries."""
    event_type = event.get("type", "")
    data_object = event.get("data", {}).get("object", {})
    try:
        if event_type == "checkout.session.completed":
            return _apply_checkout_completed(data_object)
        if event_type in ("customer.subscription.created",
                          "customer.subscription.updated"):
            return _apply_subscription_change(data_object)
        if event_type == "customer.subscription.deleted":
            return _apply_subscription_deleted(data_object)
        if event_type == "invoice.payment_failed":
            return _apply_payment_failed(data_object)
    except Exception as e:
        log.warning("webhook %s failed: %s", event_type, e)
        return {"ok": False, "error": str(e)}
    return {"ok": True, "ignored": event_type}


def _apply_checkout_completed(session: dict) -> dict:
    user_id = (session.get("client_reference_id")
               or (session.get("metadata") or {}).get("narve_user_id"))
    if not user_id:
        return {"ok": False, "error": "no user_id on session"}
    customer = session.get("customer")
    subscription_id = session.get("subscription")
    tier = (session.get("metadata") or {}).get("narve_tier") or "pro"
    db.upsert_billing(
        user_id=user_id, tier=tier,
        stripe_customer_id=customer, stripe_subscription_id=subscription_id,
        status="active", current_period_end=None,
    )
    return {"ok": True, "user_id": user_id, "tier": tier}


def _apply_subscription_change(sub: dict) -> dict:
    customer = sub.get("customer")
    if not customer:
        return {"ok": False, "error": "no customer"}
    row = db.get_billing_by_customer(customer)
    if not row:
        return {"ok": False, "error": "no user mapped to this customer"}
    # Tier mapping: prefer metadata, fall back to checking the price.
    price = (sub.get("items", {}).get("data") or [{}])[0].get("price", {}).get("id", "")
    tier = "pro"
    if price == os.environ.get("STRIPE_PRICE_WEALTH", ""):
        tier = "wealth"
    elif price == os.environ.get("STRIPE_PRICE_PRO", ""):
        tier = "pro"
    db.upsert_billing(
        user_id=row["user_id"], tier=tier,
        stripe_customer_id=customer,
        stripe_subscription_id=sub.get("id"),
        status=sub.get("status") or "active",
        current_period_end=str(sub.get("current_period_end") or ""),
    )
    return {"ok": True, "user_id": row["user_id"], "tier": tier}


def _apply_subscription_deleted(sub: dict) -> dict:
    customer = sub.get("customer")
    row = db.get_billing_by_customer(customer) if customer else None
    if not row:
        return {"ok": False, "error": "no mapping"}
    db.upsert_billing(
        user_id=row["user_id"], tier="free",
        stripe_customer_id=customer,
        stripe_subscription_id=None, status="cancelled",
        current_period_end=None,
    )
    return {"ok": True, "user_id": row["user_id"], "downgrade": "free"}


def _apply_payment_failed(invoice: dict) -> dict:
    customer = invoice.get("customer")
    row = db.get_billing_by_customer(customer) if customer else None
    if not row:
        return {"ok": False, "error": "no mapping"}
    db.upsert_billing(
        user_id=row["user_id"], tier=row.get("tier") or "free",
        stripe_customer_id=customer,
        stripe_subscription_id=row.get("stripe_subscription_id"),
        status="past_due", current_period_end=row.get("current_period_end"),
    )
    return {"ok": True, "user_id": row["user_id"], "status": "past_due"}

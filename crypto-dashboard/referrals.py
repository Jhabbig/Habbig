#!/usr/bin/env python3
"""
Referral codes + conversion attribution.

How it works:
  1. Every authenticated user has a stable, random referral code derived
     on first lookup (8 chars, ~40 bits of entropy — uniqueness checked
     against the table, not guessable).
  2. When a visitor lands on the site with `?ref=CODE`, the frontend
     POSTs to /api/referrals/track which stores the (visitor_anon_id,
     code) attribution as 'pending'.
  3. When the visitor signs up (gateway flow we don't control), the
     gateway eventually authenticates them and they hit our backend. We
     try to bind the prior anon attribution to their real user_id via
     a short-lived cookie.
  4. When that user upgrades to Pro/Wealth, the Stripe webhook handler
     also calls `record_conversion(user_id, tier, value_cents)` which
     marks the attribution converted.
  5. The referrer's dashboard sums up converted referrals × payout rate
     so they see their earnings in real time.

Security model:
  - Codes are random 8-char base32 — not guessable, not sequential.
  - Self-referrals blocked: if attribution.referrer == referred user,
    we drop it.
  - Conversions verified through Stripe webhook only (already
    signature-checked in billing.py). No way to forge from the frontend.
  - Attribution rows are immutable after recording — we add a converted
    timestamp + value, never rewrite the linkage.
  - Public stats only show count + total $, never list individual
    referred user IDs.

Payout model:
  - 20% rev share on the first 12 months of each Pro/Wealth conversion.
  - Stored as cents in `payout_owed_cents`; admin tooling can flush
    these to Stripe coupons or PayPal in batch.
"""

from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Optional

import database as db

log = logging.getLogger("crypto.referrals")

# 8 chars from a 32-char alphabet = 40 bits of entropy ≈ 1.1 trillion codes.
# Crockford base32 minus ambiguous chars (no 0/O, 1/I/L, U).
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

# Revenue share applied to each referred user's monthly payment, capped at
# 12 months per attribution (`max_payout_months`).
PAYOUT_RATE = 0.20
PAYOUT_MAX_MONTHS = 12

# Tier monthly value (cents). Used to compute payout when Stripe doesn't
# echo the exact amount back (e.g. trial conversions, discounts).
TIER_MONTHLY_CENTS = {"pro": 2500, "wealth": 7500}


# ─── Code generation ────────────────────────────────────────────────────────

def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def get_or_create_code(user_id: str) -> str:
    """Return the user's referral code, generating a new one (with collision
    retry) if they don't have one yet."""
    row = db.get_referral_code(user_id)
    if row:
        return row["code"]
    # Generate + insert with collision retry.
    for _ in range(10):
        code = _generate_code()
        if db.try_insert_referral_code(user_id, code):
            return code
    # Astronomically unlikely (would need ~33 collisions in a row), but be
    # defensive: append two extra chars rather than crash.
    for _ in range(5):
        code = _generate_code() + secrets.choice(CODE_ALPHABET) + secrets.choice(CODE_ALPHABET)
        if db.try_insert_referral_code(user_id, code):
            return code
    raise RuntimeError("could not allocate unique referral code")


def resolve_code(code: str) -> Optional[str]:
    """Return the user_id for a referral code, or None if it doesn't exist."""
    if not code or not isinstance(code, str):
        return None
    code = code.strip().upper()
    # Reject malformed codes early — saves DB hits.
    if len(code) < 6 or len(code) > 12:
        return None
    if not all(c in CODE_ALPHABET for c in code):
        return None
    row = db.get_referral_by_code(code)
    return row["user_id"] if row else None


# ─── Attribution ────────────────────────────────────────────────────────────

def track_visit(code: str, anon_id: str, source: str = "link") -> dict:
    """Record that an anonymous visitor arrived via `code`. We don't yet
    know who they are — we'll bind on signup. Returns {ok, referrer_user_id?}."""
    referrer = resolve_code(code)
    if not referrer:
        return {"ok": False, "error": "unknown code"}
    db.insert_referral_visit(referrer_user_id=referrer, anon_id=anon_id,
                              referral_code=code.upper(), source=source[:40])
    return {"ok": True, "referrer_user_id": referrer}


def bind_visit_to_user(anon_id: str, user_id: str) -> Optional[int]:
    """Called once after a visitor signs up. If they have a prior visit row,
    we promote it to a full attribution. Self-referrals are dropped."""
    visit = db.get_latest_unbound_visit(anon_id)
    if not visit:
        return None
    if visit["referrer_user_id"] == user_id:
        # Self-referral — silently drop. No abuse signal here, the gateway
        # legitimately routes us back to ourselves sometimes.
        db.delete_referral_visit(visit["id"])
        return None
    # Existing attribution? Only one per referred user; first-touch wins.
    existing = db.get_attribution_by_referred(user_id)
    if existing:
        return existing["id"]
    aid = db.insert_referral_attribution(
        referred_user_id=user_id,
        referrer_user_id=visit["referrer_user_id"],
        referral_code=visit["referral_code"],
        source=visit["source"] or "link",
    )
    db.delete_referral_visit(visit["id"])
    return aid


def record_conversion(referred_user_id: str, tier: str,
                       monthly_value_cents: Optional[int] = None) -> Optional[int]:
    """Mark an attribution as converted and accrue the payout. Idempotent —
    if we've already recorded a conversion for this user, we don't double-
    pay. Called from the Stripe webhook handler."""
    if tier not in TIER_MONTHLY_CENTS:
        return None
    row = db.get_attribution_by_referred(referred_user_id)
    if not row:
        return None
    if row.get("converted_at"):
        # Already converted; bump the tier if they upgraded but don't
        # re-credit the initial payout (cron handles ongoing months).
        if tier != (row.get("conversion_tier") or "").lower():
            db.update_attribution_tier(row["id"], tier)
        return row["id"]
    value = monthly_value_cents or TIER_MONTHLY_CENTS[tier]
    payout = int(value * PAYOUT_RATE)
    db.mark_attribution_converted(
        attribution_id=row["id"], conversion_tier=tier,
        conversion_value_cents=value,
        payout_owed_cents=payout,
    )
    return row["id"]


# ─── Stats (referrer-facing) ────────────────────────────────────────────────

def my_stats(user_id: str) -> dict:
    """Aggregate stats for the referrer's dashboard."""
    code = get_or_create_code(user_id)
    visits = db.count_referral_visits(user_id)
    attributions = db.get_attributions_by_referrer(user_id)
    converted = [a for a in attributions if a.get("converted_at")]
    pending_payout = sum(int(a.get("payout_owed_cents") or 0) for a in attributions
                         if (a.get("payout_status") or "pending") == "pending")
    paid_out = sum(int(a.get("payout_owed_cents") or 0) for a in attributions
                   if a.get("payout_status") == "paid")
    return {
        "code": code,
        "share_url": f"https://crypto.narve.ai/?ref={code}",
        "visit_count": visits,
        "signup_count": len(attributions),
        "conversion_count": len(converted),
        "conversion_rate": (len(converted) / len(attributions)) if attributions else 0.0,
        "pending_payout_cents": pending_payout,
        "paid_out_cents": paid_out,
        "lifetime_payout_cents": pending_payout + paid_out,
    }

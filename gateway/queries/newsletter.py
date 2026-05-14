"""Queries extracted from gateway/db.py — newsletter domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.

Domain split:
  * ``subscribe_newsletter``       — insert/upsert + position math + double-opt-in
                                     bookkeeping for the pre-release waitlist.
  * ``get_newsletter_position``    — read-only position lookup for returning
                                     visitors.
  * ``confirm_newsletter``         — flip ``confirmed_at`` after a successful
                                     verification-token click.
  * ``unsubscribe_newsletter``     — flip ``unsubscribed_at`` from the
                                     one-click footer link.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Optional

import db


# ── shared helpers ─────────────────────────────────────────────────────────


# Valid segments — kept in sync with the prerelease/landing form options.
# Surface-level validation lives in public_routes.py; this constant exists
# so callers (admin tools, future tests) can introspect the legal set
# without grepping the route handler.
VALID_SEGMENTS = ("all", "markets", "election", "climate", "intelligence")
VALID_FREQUENCIES = ("weekly", "monthly", "daily_spike")

# Resend cooldown: never send a second confirmation email to the same
# pending row within 24h. The signup endpoint returns an identical 200
# regardless, so a caller can't tell whether the email is already pending.
CONFIRMATION_RESEND_COOLDOWN_S = 86_400


def _new_referral_code() -> str:
    """Generate a short, URL-safe referral code. Collision odds are ~1/10^14
    per code; the caller handles the rare IntegrityError retry.
    """
    return secrets.token_urlsafe(6)[:8]


def _new_confirmation_token() -> str:
    """Generate a signed, URL-safe confirmation token.

    Format: ``<raw>.<sig>`` where ``sig`` is HMAC-SHA256 of the raw bytes
    keyed by ``GATEWAY_COOKIE_SECRET`` (truncated to 32 hex chars). Mirrors
    the shape used by ``email_system.unsubscribe`` so the verification
    helper has the same shape across the codebase.

    Tokens are 24 raw bytes (urlsafe-base64) plus a 32-char signature —
    short enough to fit in an email-friendly URL, long enough that
    brute-forcing the space is computationally infeasible.
    """
    secret = os.environ.get("GATEWAY_COOKIE_SECRET", "narve-newsletter-confirm").encode()
    raw = secrets.token_urlsafe(24)
    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _verify_confirmation_token(token: str) -> bool:
    """Re-derive the signature and constant-time compare against the
    presented token. Returns False on malformed input rather than raising
    so the route handler can return a clean 400.
    """
    if not token or "." not in token:
        return False
    raw, sig = token.rsplit(".", 1)
    secret = os.environ.get("GATEWAY_COOKIE_SECRET", "narve-newsletter-confirm").encode()
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


# ── public API ─────────────────────────────────────────────────────────────


def subscribe_newsletter(
    email: str,
    source: str = "prerelease",
    referred_by: Optional[str] = None,
    segment: str = "all",
    frequency: str = "weekly",
) -> dict:
    """Insert or fetch a newsletter row and return waitlist + confirmation metadata.

    Return shape:
        {
            "is_new": bool,                       # False if email already existed
            "referral_code": str,                 # always present (backfilled if old row)
            "referred_by": str | None,            # inviter's referral_code, if any
            "position": int,                      # 1-indexed waitlist position
            "confirmation_required": bool,        # True if the caller should send the confirm email
            "confirmation_token": str | None,     # present when confirmation_required is True
            "segment": str,                       # canonical segment stored
            "frequency": str,                     # canonical frequency stored
        }

    Confirmation semantics:
      * Brand-new email          → confirmation_required=True, fresh token issued.
      * Existing unconfirmed row → confirmation_required=True ONLY if the last
                                   send was more than CONFIRMATION_RESEND_COOLDOWN_S
                                   seconds ago; otherwise the route still returns
                                   200 but won't trigger another email.
      * Existing confirmed row   → confirmation_required=False; preferences (segment,
                                   frequency) are updated silently.

    Caller is responsible for actually enqueueing the email when
    ``confirmation_required`` is True. The DB only tracks intent.

    The ``referred_by`` argument must match an existing subscriber's
    referral_code — invalid values are silently ignored so a malformed
    ?ref= never 500s the signup form.

    ``segment`` and ``frequency`` are clamped to ``VALID_SEGMENTS`` /
    ``VALID_FREQUENCIES`` here as a defence-in-depth check; the route
    handler validates first and serves a 400 on garbage input.
    """
    email = (email or "").strip().lower()
    now = int(time.time())

    # Clamp segment / frequency to known values as a defence-in-depth check.
    # Route handler validates first, but a future caller could bypass that.
    if segment not in VALID_SEGMENTS:
        segment = "all"
    if frequency not in VALID_FREQUENCIES:
        frequency = "weekly"

    # Normalise the inviter code: only accept exact matches on an existing row.
    inviter_code: Optional[str] = None
    if referred_by:
        referred_by = referred_by.strip()
        if referred_by:
            with db.conn() as c:
                row = c.execute(
                    "SELECT 1 FROM newsletter_subscribers WHERE referral_code = ? LIMIT 1",
                    (referred_by,),
                ).fetchone()
                if row:
                    inviter_code = referred_by

    with db.conn() as c:
        existing = c.execute(
            "SELECT id, referral_code, confirmed_at, last_confirmation_sent_at, "
            "confirmation_token, segment, frequency, unsubscribed_at "
            "FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()

        if existing:
            # Idempotent re-signup. Three sub-paths:
            #
            #  1. Row is confirmed → silently update segment/frequency,
            #     never re-send a confirmation email. Even if the user
            #     also re-clicked an old confirmation link, it's a no-op.
            #
            #  2. Row is unconfirmed, cooldown elapsed → reissue a fresh
            #     token, update preferences, set confirmation_required so
            #     the caller enqueues the email. last_confirmation_sent_at
            #     is updated to start a new cooldown window.
            #
            #  3. Row is unconfirmed, cooldown active → return identical
            #     200 shape but confirmation_required=False. This is the
            #     anti-enumeration property — a probe can't distinguish
            #     "email pending" from "email never seen" by timing.
            ref_code = existing["referral_code"]
            if not ref_code:
                # Defensive: backfill if init_db's migration missed a row.
                ref_code = _new_referral_code()
                c.execute(
                    "UPDATE newsletter_subscribers SET referral_code = ? WHERE id = ?",
                    (ref_code, existing["id"]),
                )

            # If they previously unsubscribed, a re-subscribe wipes the flag
            # and runs them through double-opt-in again. GDPR-clean.
            was_unsubscribed = existing["unsubscribed_at"] is not None
            if was_unsubscribed:
                c.execute(
                    "UPDATE newsletter_subscribers SET unsubscribed_at = NULL, "
                    "confirmed_at = NULL WHERE id = ?",
                    (existing["id"],),
                )
                # Re-fetch with cleared confirmed_at — treat as fresh signup below.
                existing_confirmed = None
            else:
                existing_confirmed = existing["confirmed_at"]

            # Always reflect the latest preference choice — letting people
            # tighten down their subscription is the GDPR-friendly default.
            c.execute(
                "UPDATE newsletter_subscribers SET segment = ?, frequency = ? WHERE id = ?",
                (segment, frequency, existing["id"]),
            )

            confirmation_required = False
            confirmation_token: Optional[str] = None

            if existing_confirmed is None:
                # Unconfirmed — check cooldown.
                last_sent = existing["last_confirmation_sent_at"] or 0
                cooldown_elapsed = (now - last_sent) >= CONFIRMATION_RESEND_COOLDOWN_S
                if cooldown_elapsed:
                    confirmation_token = _new_confirmation_token()
                    c.execute(
                        "UPDATE newsletter_subscribers SET confirmation_token = ?, "
                        "last_confirmation_sent_at = ? WHERE id = ?",
                        (confirmation_token, now, existing["id"]),
                    )
                    confirmation_required = True

            return {
                "is_new": False,
                "referral_code": ref_code,
                "referred_by": None,
                "position": _waitlist_position(c, ref_code),
                "confirmation_required": confirmation_required,
                "confirmation_token": confirmation_token,
                "segment": segment,
                "frequency": frequency,
            }

        # New signup — issue a referral code and a confirmation token in one
        # insert. Retry on the rare referral_code collision.
        ref_code = _new_referral_code()
        confirmation_token = _new_confirmation_token()

        for _ in range(5):
            try:
                c.execute(
                    "INSERT INTO newsletter_subscribers "
                    "(email, subscribed_at, source, referral_code, referred_by, "
                    " segment, frequency, confirmation_token, last_confirmation_sent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (email, now, source, ref_code, inviter_code,
                     segment, frequency, confirmation_token, now),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "referral_code" in str(exc):
                    ref_code = _new_referral_code()
                    continue
                # Email unique conflict under concurrent signup — treat as
                # re-signup on the next SELECT below.
                existing = c.execute(
                    "SELECT id, referral_code, confirmed_at, confirmation_token "
                    "FROM newsletter_subscribers WHERE email = ?",
                    (email,),
                ).fetchone()
                if existing:
                    return {
                        "is_new": False,
                        "referral_code": existing["referral_code"] or ref_code,
                        "referred_by": None,
                        "position": _waitlist_position(c, existing["referral_code"] or ref_code),
                        "confirmation_required": False,
                        "confirmation_token": None,
                        "segment": segment,
                        "frequency": frequency,
                    }
                raise

        return {
            "is_new": True,
            "referral_code": ref_code,
            "referred_by": inviter_code,
            "position": _waitlist_position(c, ref_code),
            "confirmation_required": True,
            "confirmation_token": confirmation_token,
            "segment": segment,
            "frequency": frequency,
        }


def _waitlist_position(c, referral_code: str) -> int:
    """Return this subscriber's 1-indexed position on the waitlist.

    Rank = number of subscribers who signed up at-or-before this one
    (ordered by subscribed_at, tie-broken by id). Each successful referral
    the subscriber has made bumps them forward by one slot. Floor at 1 so
    nobody gets a zero or negative number.
    """
    row = c.execute(
        "SELECT id, subscribed_at FROM newsletter_subscribers WHERE referral_code = ?",
        (referral_code,),
    ).fetchone()
    if not row:
        # Total count as a safe fallback — caller shouldn't see this path.
        total = c.execute("SELECT COUNT(*) FROM newsletter_subscribers").fetchone()[0]
        return max(1, total)

    rank = c.execute(
        "SELECT COUNT(*) FROM newsletter_subscribers "
        "WHERE subscribed_at < ? OR (subscribed_at = ? AND id <= ?)",
        (row["subscribed_at"], row["subscribed_at"], row["id"]),
    ).fetchone()[0]

    referrals = c.execute(
        "SELECT COUNT(*) FROM newsletter_subscribers WHERE referred_by = ?",
        (referral_code,),
    ).fetchone()[0]

    return max(1, rank - referrals)


def get_newsletter_position(email: str) -> Optional[dict]:
    """Look up an existing subscriber's current waitlist position.

    Returns None if the email isn't on the waitlist. Used by
    /api/newsletter/position so returning visitors can see their current
    rank after their link has been used.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with db.conn() as c:
        row = c.execute(
            "SELECT referral_code, referred_by FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return None
        ref_code = row["referral_code"]
        if not ref_code:
            # Backfill lazily so subsequent position calls are stable.
            ref_code = _new_referral_code()
            c.execute(
                "UPDATE newsletter_subscribers SET referral_code = ? WHERE email = ?",
                (ref_code, email),
            )
        return {
            "is_new": False,
            "referral_code": ref_code,
            "referred_by": row["referred_by"],
            "position": _waitlist_position(c, ref_code),
        }


def confirm_newsletter(token: str) -> Optional[dict]:
    """Apply a confirmation token. Returns the confirmed row dict on success,
    None on bad / expired / already-used tokens.

    Single-use semantics: on success we set ``confirmed_at`` and wipe
    ``confirmation_token`` so re-clicking the link from a forwarded email
    is a clean no-op rather than a "this looks broken" error.

    Anti-timing: this function does NOT reveal *why* it failed (bad sig vs
    no matching row vs already-confirmed). The route handler renders the
    same confirmation page for any failure mode.
    """
    if not _verify_confirmation_token(token):
        return None
    now = int(time.time())
    with db.conn() as c:
        row = c.execute(
            "SELECT id, email, confirmed_at, segment, frequency "
            "FROM newsletter_subscribers WHERE confirmation_token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE newsletter_subscribers "
            "SET confirmed_at = ?, confirmation_token = NULL "
            "WHERE id = ?",
            (now, row["id"]),
        )
        return {
            "email": row["email"],
            "segment": row["segment"],
            "frequency": row["frequency"],
            "confirmed_at": now,
            # Already-confirmed rows still return a success-shaped dict so
            # the page doesn't look like an error to a user who clicked twice.
            "was_already_confirmed": row["confirmed_at"] is not None,
        }


def unsubscribe_newsletter(email: str) -> bool:
    """Mark a newsletter row unsubscribed. Returns True if a row was
    updated, False if the email isn't on the list. Used by the one-click
    footer link.

    Implementation note: we don't delete the row — keeping it lets us
    suppress future re-sends (e.g. someone re-imported from a stale list)
    and honour the unsubscribe across deletes. GDPR-clean because the
    email remains only for suppression purposes.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    now = int(time.time())
    with db.conn() as c:
        result = c.execute(
            "UPDATE newsletter_subscribers SET unsubscribed_at = ? WHERE email = ?",
            (now, email),
        )
        return result.rowcount > 0


__all__ = [
    'subscribe_newsletter',
    'get_newsletter_position',
    'confirm_newsletter',
    'unsubscribe_newsletter',
    'VALID_SEGMENTS',
    'VALID_FREQUENCIES',
    'CONFIRMATION_RESEND_COOLDOWN_S',
]

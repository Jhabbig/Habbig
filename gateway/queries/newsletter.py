"""Queries extracted from gateway/db.py — newsletter domain.

Moved out of db.py to keep the connection-pooling/schema module small.
Re-exported back onto db.py at import time, so every existing
``import db; db.<name>`` call site keeps working unchanged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import time
from typing import Optional

import db


def _new_referral_code() -> str:
    """Generate a short, URL-safe referral code. Collision odds are ~1/10^14
    per code; the caller handles the rare IntegrityError retry.
    """
    return secrets.token_urlsafe(6)[:8]


def subscribe_newsletter(
    email: str,
    source: str = "prerelease",
    referred_by: Optional[str] = None,
) -> dict:
    """Insert or fetch a newsletter row and return waitlist metadata.

    Return shape:
        {
            "is_new": bool,              # False if email already existed
            "referral_code": str,        # always present (backfilled if old row)
            "referred_by": str | None,   # inviter's referral_code, if any
            "position": int,             # 1-indexed waitlist position
        }

    Position is computed as:
        subscriber_rank - 5 * num_successful_referrals
    floored at 1. Rank is the 1-indexed row order by subscribed_at, so
    new signups always start at the back and climb as their link gets used.

    The referred_by argument must match an existing subscriber's
    referral_code — invalid values are silently ignored so a malformed
    ?ref= never 500s the signup form.
    """
    email = (email or "").strip().lower()
    now = int(time.time())

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
            "SELECT id, referral_code FROM newsletter_subscribers WHERE email = ?",
            (email,),
        ).fetchone()

        if existing:
            # Idempotent re-signup — don't touch source/referred_by, just
            # return the current position so the UI shows the same number.
            ref_code = existing["referral_code"]
            if not ref_code:
                # Defensive: backfill if init_db's migration missed a row.
                ref_code = _new_referral_code()
                c.execute(
                    "UPDATE newsletter_subscribers SET referral_code = ? WHERE id = ?",
                    (ref_code, existing["id"]),
                )
            return {
                "is_new": False,
                "referral_code": ref_code,
                "referred_by": None,
                "position": _waitlist_position(c, ref_code),
            }

        # New signup — retry on the rare referral_code collision.
        ref_code = _new_referral_code()
        for _ in range(5):
            try:
                c.execute(
                    "INSERT INTO newsletter_subscribers "
                    "(email, subscribed_at, source, referral_code, referred_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (email, now, source, ref_code, inviter_code),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "referral_code" in str(exc):
                    ref_code = _new_referral_code()
                    continue
                # Email unique conflict under concurrent signup — treat as
                # re-signup on the next SELECT below.
                existing = c.execute(
                    "SELECT id, referral_code FROM newsletter_subscribers WHERE email = ?",
                    (email,),
                ).fetchone()
                if existing:
                    return {
                        "is_new": False,
                        "referral_code": existing["referral_code"] or ref_code,
                        "referred_by": None,
                        "position": _waitlist_position(c, existing["referral_code"] or ref_code),
                    }
                raise

        return {
            "is_new": True,
            "referral_code": ref_code,
            "referred_by": inviter_code,
            "position": _waitlist_position(c, ref_code),
        }


def _waitlist_position(c, referral_code: str) -> int:
    """Return this subscriber's 1-indexed position on the waitlist.

    Rank = number of subscribers who signed up at-or-before this one
    (ordered by subscribed_at, tie-broken by id). Each successful referral
    the subscriber has made bumps them forward by 5 slots. Floor at 1 so
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

    return max(1, rank - 5 * referrals)


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


__all__ = [
    'subscribe_newsletter',
    'get_newsletter_position',
]

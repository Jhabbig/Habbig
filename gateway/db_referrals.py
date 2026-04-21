"""DB layer for the private referral program + opt-in leaderboard.

Parallel sibling to `db.py`. Kept separate so the new feature's schema can
ship without touching the 3700-line main db module — and, more importantly,
so a re-sync of `db.py` from an upstream branch can't wipe these helpers.
Everything here uses `db.conn()` to share the same sqlite connection /
WAL-mode semantics as the rest of the codebase; there is no second
datastore.

All functions assume migration 023_referrals_leaderboard has run — it adds:
  * `users.referral_code`, `referred_by_user_id`,
    `referral_credits_earned_months`, `leaderboard_participation`,
    `leaderboard_handle`
  * `referrals` table
  * `user_accuracy` table

If any of those are missing at call time, the helpers raise a plain
sqlite3 error; they are not defensive about "table not found" because
that would mask a real migration-ordering bug.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from typing import Optional

import db


# Alphabet excludes 0/1/I/O (visual ambiguity) so a code read off a screen
# or typed by hand doesn't need guesswork. Uppercase L stays — it's clearly
# different from 1 at any reasonable font.
_REFERRAL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ── Referral codes + user lookups ────────────────────────────────────────────


def generate_referral_code() -> str:
    """Return a 10-char URL-safe code. Collision odds are negligible at our
    scale (32^10 ≈ 1e15) but `ensure_user_referral_code` still retries on
    the rare UNIQUE index collision."""
    return "".join(secrets.choice(_REFERRAL_CODE_ALPHABET) for _ in range(10))


def ensure_user_referral_code(user_id: int) -> str:
    """Idempotent: returns the user's referral_code, generating + persisting
    one on first call. Existing users get a code the first time they open
    /settings/referrals."""
    with db.conn() as c:
        row = c.execute(
            "SELECT referral_code FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if row and row["referral_code"]:
            return row["referral_code"]
        for _ in range(8):
            code = generate_referral_code()
            try:
                c.execute(
                    "UPDATE users SET referral_code = ? "
                    "WHERE id = ? AND (referral_code IS NULL OR referral_code = '')",
                    (code, user_id),
                )
                # Re-read to handle the race where another request just
                # wrote a different code for the same user.
                fresh = c.execute(
                    "SELECT referral_code FROM users WHERE id = ?", (user_id,),
                ).fetchone()
                if fresh and fresh["referral_code"]:
                    return fresh["referral_code"]
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError(
            f"could not assign referral code to user {user_id} after 8 tries"
        )


def get_user_by_referral_code(code: str) -> Optional[sqlite3.Row]:
    """Public-link resolution. Case-insensitive on input — codes are stored
    uppercase so a casually-copied URL still works."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE referral_code = ? "
            "AND COALESCE(suspended, 0) = 0 AND COALESCE(is_deleted, 0) = 0",
            (code,),
        ).fetchone()


# ── Referral row lifecycle ───────────────────────────────────────────────────


def create_referral(
    *,
    referrer_user_id: int,
    referred_email: Optional[str] = None,
    referred_user_id: Optional[int] = None,
    invite_token_id: Optional[int] = None,
) -> int:
    """Create a pending row. `converted_to_paid` + `reward_granted` default
    to 0; the daily job flips both downstream."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO referrals "
            "(referrer_user_id, referred_user_id, referred_email, "
            " invite_token_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                referrer_user_id,
                referred_user_id,
                (referred_email or "").strip().lower() or None,
                invite_token_id,
                int(time.time()),
            ),
        )
        return cur.lastrowid


def attach_user_to_referral(referral_id: int, user_id: int) -> bool:
    """Called when the invitee actually registers. Fills referred_user_id
    IF NULL — so re-running a bug doesn't rebind the row."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE referrals SET referred_user_id = ? "
            "WHERE id = ? AND referred_user_id IS NULL",
            (user_id, referral_id),
        )
        return cur.rowcount > 0


def mark_referral_converted(referred_user_id: int) -> int:
    """Flip every pending referral for the user to converted. Idempotent —
    running twice is safe because `converted_to_paid = 0` filters out
    already-flipped rows."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE referrals SET converted_to_paid = 1, converted_at = ? "
            "WHERE referred_user_id = ? AND converted_to_paid = 0",
            (int(time.time()), referred_user_id),
        )
        return cur.rowcount


def get_user_referrals(referrer_user_id: int) -> list[sqlite3.Row]:
    """All referrals the user has sent, most recent first.
    LEFT JOIN users so the UI can show invitee username/email without a
    second DB round-trip per row."""
    with db.conn() as c:
        return c.execute(
            "SELECT r.id, r.referred_email, r.referred_user_id, "
            "       r.created_at, r.converted_to_paid, r.converted_at, "
            "       r.reward_granted, r.reward_type, r.reward_months, "
            "       r.reward_tier, "
            "       u.username AS referred_username, "
            "       u.email AS referred_user_email "
            "FROM referrals r "
            "LEFT JOIN users u ON u.id = r.referred_user_id "
            "WHERE r.referrer_user_id = ? "
            "ORDER BY r.created_at DESC, r.id DESC",
            (referrer_user_id,),
        ).fetchall()


def count_converted_referrals(referrer_user_id: int) -> int:
    """How many of this user's referrals have actually paid."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM referrals "
            "WHERE referrer_user_id = ? AND converted_to_paid = 1",
            (referrer_user_id,),
        ).fetchone()
    return int(row["n"] if row else 0)


# ── Reward-granting queue helpers (consumed by the daily job) ───────────────


def list_pending_reward_referrals(limit: int = 500) -> list[sqlite3.Row]:
    """Drained by process_referral_rewards. Oldest-first so the stacking
    logic numbers conversions in the order they became paying."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM referrals "
            "WHERE converted_to_paid = 1 AND reward_granted = 0 "
            "ORDER BY converted_at ASC LIMIT ?",
            (limit,),
        ).fetchall()


def mark_referral_reward_granted(
    referral_id: int,
    *,
    reward_type: str,
    reward_months: int,
    reward_tier: Optional[str],
    gifted_subscription_id: Optional[int],
) -> bool:
    """Atomic stamp. Returns True iff the update actually happened (row was
    previously un-granted). Lets the caller detect a race and revoke an
    orphan gift before re-running."""
    with db.conn() as c:
        cur = c.execute(
            "UPDATE referrals "
            "SET reward_granted = 1, reward_granted_at = ?, "
            "    reward_type = ?, reward_months = ?, reward_tier = ?, "
            "    gifted_subscription_id = ? "
            "WHERE id = ? AND reward_granted = 0",
            (
                int(time.time()),
                reward_type,
                reward_months,
                reward_tier,
                gifted_subscription_id,
                referral_id,
            ),
        )
        return cur.rowcount > 0


def add_referral_credit_months(user_id: int, months: int) -> None:
    """Display counter on /settings/referrals. Entitlement lives in the
    `gifted_subscriptions` row we just inserted — this is *only* for UI."""
    if months <= 0:
        return
    with db.conn() as c:
        c.execute(
            "UPDATE users "
            "SET referral_credits_earned_months = "
            "    COALESCE(referral_credits_earned_months, 0) + ? "
            "WHERE id = ?",
            (months, user_id),
        )


def get_referral_stats(user_id: int) -> dict:
    """Aggregate for /api/referrals/me — one query, one JOIN."""
    with db.conn() as c:
        row = c.execute(
            "SELECT "
            "  COUNT(*) AS total_sent, "
            "  SUM(CASE WHEN converted_to_paid = 1 THEN 1 ELSE 0 END) AS total_converted, "
            "  SUM(CASE WHEN reward_granted = 1 THEN 1 ELSE 0 END) AS total_rewarded, "
            "  SUM(COALESCE(reward_months, 0)) AS total_reward_months "
            "FROM referrals WHERE referrer_user_id = ?",
            (user_id,),
        ).fetchone()
        u = c.execute(
            "SELECT referral_code, referral_credits_earned_months "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return {
        "referral_code": u["referral_code"] if u else None,
        "credits_earned_months": int(u["referral_credits_earned_months"] or 0)
            if u else 0,
        "total_sent": int(row["total_sent"] or 0),
        "total_converted": int(row["total_converted"] or 0),
        "total_rewarded": int(row["total_rewarded"] or 0),
        "total_reward_months": int(row["total_reward_months"] or 0),
    }


# ── Gifted-subscription insertion (used by the reward job) ──────────────────


def insert_referral_gift(
    *,
    user_id: int,
    subscription_type: str,
    months: int,
    internal_notes: str,
) -> int:
    """Insert a gifted_subscriptions row for a referral reward. Returns
    the gift id. Kept here (not in the job) so the schema coupling is in
    one place — if another migration renames the gifted_subscriptions
    columns, this single function is the only thing that breaks."""
    now = int(time.time())
    ends_at = now + months * 30 * 86400
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO gifted_subscriptions "
            "(user_id, gifted_by_admin_id, subscription_type, is_enterprise, "
            " starts_at, ends_at, is_permanent, internal_notes, created_at) "
            "VALUES (?, NULL, ?, 0, ?, ?, 0, ?, ?)",
            (user_id, subscription_type, now, ends_at, internal_notes, now),
        )
        return cur.lastrowid


def revoke_orphan_gift(gift_id: int) -> None:
    """Called when `mark_referral_reward_granted` lost the stamp race.
    Flips the gift row to revoked so the user doesn't get double-credit
    on top of whichever concurrent worker actually stamped the referral."""
    with db.conn() as c:
        c.execute(
            "UPDATE gifted_subscriptions SET revoked = 1, revoked_at = ?, "
            "internal_notes = COALESCE(internal_notes, '') "
            "  || ' [orphaned by race; auto-revoked]' "
            "WHERE id = ?",
            (int(time.time()), gift_id),
        )


# ── Leaderboard opt-in + scoring ─────────────────────────────────────────────


def set_leaderboard_participation(
    user_id: int,
    *,
    participate: bool,
    display_name: Optional[str] = None,
) -> dict:
    """Opt-in / opt-out. Handle rules:
      - 3-24 chars, alphanumeric + underscore + dash
      - Unique across participants (UNIQUE partial index enforces)
      - Stored as-submitted (case preserved)

    Returns {"ok": bool, "error": str|None}. The API route maps a 'taken'
    error to HTTP 409 and anything else to 400."""
    if not participate:
        with db.conn() as c:
            c.execute(
                "UPDATE users SET leaderboard_participation = 0 WHERE id = ?",
                (user_id,),
            )
        return {"ok": True, "error": None}

    handle = (display_name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,24}", handle):
        return {
            "ok": False,
            "error": "Display name must be 3-24 chars (letters, digits, _ or -).",
        }
    with db.conn() as c:
        try:
            c.execute(
                "UPDATE users SET leaderboard_participation = 1, "
                "leaderboard_handle = ? WHERE id = ?",
                (handle, user_id),
            )
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That display name is taken."}
    return {"ok": True, "error": None}


def get_leaderboard_opt_in(user_id: int) -> dict:
    """For the /settings privacy panel."""
    with db.conn() as c:
        row = c.execute(
            "SELECT leaderboard_participation, leaderboard_handle "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"participating": False, "handle": None}
    return {
        "participating": bool(row["leaderboard_participation"]),
        "handle": row["leaderboard_handle"],
    }


def upsert_user_accuracy(
    user_id: int,
    *,
    total: int,
    correct: int,
    accuracy_all: Optional[float],
    accuracy_90d: Optional[float],
    accuracy_30d: Optional[float],
    accuracy_7d: Optional[float],
) -> None:
    """Called by the nightly scorer job. Full recompute — wipes previous
    accuracy with each run so a change to the metric formula rolls out by
    the next cron tick with no migration needed.
    `accuracy_score` mirrors all-time so ORDER BY accuracy_score DESC works
    as the default sort without the API having to pick a column."""
    with db.conn() as c:
        c.execute(
            "INSERT INTO user_accuracy "
            "(user_id, accuracy_score, total_predictions, correct_predictions, "
            " accuracy_all_time, accuracy_90d, accuracy_30d, accuracy_7d, "
            " last_computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  accuracy_score = excluded.accuracy_score, "
            "  total_predictions = excluded.total_predictions, "
            "  correct_predictions = excluded.correct_predictions, "
            "  accuracy_all_time = excluded.accuracy_all_time, "
            "  accuracy_90d = excluded.accuracy_90d, "
            "  accuracy_30d = excluded.accuracy_30d, "
            "  accuracy_7d = excluded.accuracy_7d, "
            "  last_computed_at = excluded.last_computed_at",
            (
                user_id,
                accuracy_all,  # accuracy_score = accuracy_all_time default
                total,
                correct,
                accuracy_all, accuracy_90d, accuracy_30d, accuracy_7d,
                int(time.time()),
            ),
        )


def get_leaderboard(
    *,
    period: str = "all",
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Opt-in users ranked by accuracy for the given period. NULL-accuracy
    rows are filtered out — those users render as 'not yet ranked' in the
    footer, never at the bottom of the table."""
    col = {
        "all": "ua.accuracy_all_time",
        "90d": "ua.accuracy_90d",
        "30d": "ua.accuracy_30d",
        "7d":  "ua.accuracy_7d",
    }.get(period, "ua.accuracy_all_time")
    with db.conn() as c:
        return c.execute(
            f"""
            SELECT u.id AS user_id, u.leaderboard_handle AS handle,
                   ua.total_predictions, ua.correct_predictions,
                   {col} AS accuracy,
                   ua.last_computed_at
              FROM users u
              JOIN user_accuracy ua ON ua.user_id = u.id
             WHERE u.leaderboard_participation = 1
               AND COALESCE(u.is_deleted, 0) = 0
               AND COALESCE(u.suspended, 0) = 0
               AND {col} IS NOT NULL
               AND ua.total_predictions > 0
             ORDER BY {col} DESC, ua.total_predictions DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()


def count_leaderboard_participants() -> int:
    """For the footer: 'X of Y active subscribers are on the leaderboard'."""
    with db.conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE leaderboard_participation = 1 "
            "AND COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(suspended, 0) = 0"
        ).fetchone()
    return int(row["n"] if row else 0)


def get_user_leaderboard_rank(
    user_id: int, period: str = "all",
) -> Optional[dict]:
    """Per-user rank banner. None = not participating OR no predictions in
    this window. Rank is computed as 1 + COUNT(users strictly ahead), so
    two users with identical accuracy both get the same rank."""
    col = {
        "all": "accuracy_all_time",
        "90d": "accuracy_90d",
        "30d": "accuracy_30d",
        "7d":  "accuracy_7d",
    }.get(period, "accuracy_all_time")
    with db.conn() as c:
        me = c.execute(
            f"SELECT {col} AS acc FROM user_accuracy WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not me or me["acc"] is None:
            return None
        ahead = c.execute(
            f"""
            SELECT COUNT(*) AS n FROM user_accuracy ua
            JOIN users u ON u.id = ua.user_id
            WHERE u.leaderboard_participation = 1
              AND COALESCE(u.is_deleted, 0) = 0
              AND COALESCE(u.suspended, 0) = 0
              AND {col} > ?
            """,
            (me["acc"],),
        ).fetchone()
    return {"rank": int(ahead["n"]) + 1, "accuracy": float(me["acc"])}

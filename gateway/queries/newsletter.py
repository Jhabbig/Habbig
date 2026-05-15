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


# ── admin blast campaigns ──────────────────────────────────────────────────
#
# These power /admin/newsletter — one-off composed blasts to confirmed
# subscribers, filtered by segment + (optional) frequency. The recurring
# weekly digest cron is a separate path; this is the manual "we have a
# launch announcement to send" surface.


def list_newsletter_campaigns(limit: int = 50) -> list[dict]:
    """Return the most recent campaigns, newest first. Used by the
    /admin/newsletter index page to show send history.

    Includes both already-sent and pending-scheduled rows so an admin
    can see what's queued. The page splits them visually.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, admin_user_id, subject, segment, frequency_filter, "
            "scheduled_at, sent_at, recipient_count, created_at "
            "FROM newsletter_campaigns "
            "ORDER BY scheduled_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def count_blast_recipients(segment: str, frequency_filter: Optional[str]) -> int:
    """Return how many confirmed subscribers match the (segment, frequency)
    filter. Used both for live previews on the compose form and for the
    recipient_count audit field on each campaign row.

    Filter semantics:
      * ``segment == 'all'``      — match any segment value.
      * ``segment == 'markets'``  — match rows where segment is 'markets' OR
                                     'all' (the catch-all bucket gets every
                                     blast regardless of segment).
      * ``frequency_filter`` NULL — no frequency filter, every confirmed row
                                     matches.
      * ``frequency_filter`` set  — strict equality on the frequency column.

    Confirmed + not unsubscribed is enforced unconditionally — we never
    blast an unconfirmed row or someone who hit unsubscribe.
    """
    seg = segment if segment in VALID_SEGMENTS else "all"
    where = ["confirmed_at IS NOT NULL", "unsubscribed_at IS NULL"]
    params: list = []

    if seg == "all":
        # No segment narrowing — every confirmed row.
        pass
    else:
        # Targeted blast: include both the explicit segment and the
        # catch-all 'all' bucket (those subscribers opted into every
        # segment by definition).
        where.append("(segment = ? OR segment = 'all')")
        params.append(seg)

    if frequency_filter and frequency_filter in VALID_FREQUENCIES:
        where.append("frequency = ?")
        params.append(frequency_filter)

    sql = "SELECT COUNT(*) FROM newsletter_subscribers WHERE " + " AND ".join(where)
    with db.conn() as c:
        return int(c.execute(sql, params).fetchone()[0])


def get_blast_recipients(
    segment: str,
    frequency_filter: Optional[str],
) -> list[dict]:
    """Return the email + segment + frequency of every confirmed subscriber
    matching the filter. Used by the send handler to drive the enqueue loop.

    Same filter semantics as ``count_blast_recipients``. Returned in
    deterministic order (id ASC) so retries enqueue identically.
    """
    seg = segment if segment in VALID_SEGMENTS else "all"
    where = ["confirmed_at IS NOT NULL", "unsubscribed_at IS NULL"]
    params: list = []

    if seg != "all":
        where.append("(segment = ? OR segment = 'all')")
        params.append(seg)

    if frequency_filter and frequency_filter in VALID_FREQUENCIES:
        where.append("frequency = ?")
        params.append(frequency_filter)

    sql = (
        "SELECT id, email, segment, frequency FROM newsletter_subscribers "
        "WHERE " + " AND ".join(where) + " ORDER BY id ASC"
    )
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def record_newsletter_campaign(
    *,
    admin_user_id: int,
    subject: str,
    body_md: str,
    segment: str,
    frequency_filter: Optional[str],
    scheduled_at: int,
    sent_at: Optional[int],
    recipient_count: int,
) -> int:
    """Insert a campaign row and return its id.

    ``sent_at`` is None for future-scheduled blasts (dispatched later) and
    the actual unix seconds for "send now" blasts (we set it at enqueue
    time — every recipient job is on the queue, retries are the queue's
    responsibility, so the campaign is "sent" once enqueued).
    """
    now = int(time.time())
    seg = segment if segment in VALID_SEGMENTS else "all"
    freq = frequency_filter if (
        frequency_filter and frequency_filter in VALID_FREQUENCIES
    ) else None
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO newsletter_campaigns "
            "(admin_user_id, subject, body_md, segment, frequency_filter, "
            " scheduled_at, sent_at, recipient_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(admin_user_id),
                (subject or "").strip(),
                body_md or "",
                seg,
                freq,
                int(scheduled_at),
                int(sent_at) if sent_at is not None else None,
                int(recipient_count),
                now,
            ),
        )
        return int(cur.lastrowid)


# ── Bounded-blast deferred-tail helpers ───────────────────────────────────
#
# The synchronous portion of /admin/newsletter/send is bounded at
# MAX_INLINE_RECIPIENTS. Any overflow is recorded as a row in
# ``newsletter_blast_jobs`` (migration 187) and drained by the
# ``newsletter_blast_tick`` cron job. These helpers are the data plane
# for that drain.

# Inline cap: how many recipients the request handler enqueues in-band
# before deferring the rest to ``newsletter_blast_jobs``. 500 fits inside
# a ~5 s SQLite write window even on a cold DB; larger blasts stall the
# admin POST and tip the rate limit.
MAX_INLINE_RECIPIENTS = 500

# Per-tick batch size — how many deferred recipients the tick worker
# enqueues on each cron pulse. With a 60 s tick this caps the deferred
# fan-out at MAX_BATCH_PER_TICK * 60 recipients/minute, which keeps the
# scheduler responsive for the rest of the registry.
MAX_BATCH_PER_TICK = 500


def get_blast_recipients_page(
    segment: str,
    frequency_filter: Optional[str],
    *,
    offset: int,
    limit: int,
) -> list[dict]:
    """Page through ``get_blast_recipients`` for the deferred-tail worker.

    Same filter semantics as ``get_blast_recipients`` — sorted by id ASC
    so successive pages are stable across ticks even when the table is
    being mutated. ``offset`` is bounded at 0+, ``limit`` is capped at
    a generous 5_000 to defang accidental "give me everything" calls.
    """
    seg = segment if segment in VALID_SEGMENTS else "all"
    where = ["confirmed_at IS NOT NULL", "unsubscribed_at IS NULL"]
    params: list = []

    if seg != "all":
        where.append("(segment = ? OR segment = 'all')")
        params.append(seg)

    if frequency_filter and frequency_filter in VALID_FREQUENCIES:
        where.append("frequency = ?")
        params.append(frequency_filter)

    safe_offset = max(0, int(offset))
    safe_limit = min(max(1, int(limit)), 5_000)
    params.extend([safe_limit, safe_offset])

    sql = (
        "SELECT id, email, segment, frequency FROM newsletter_subscribers "
        "WHERE " + " AND ".join(where)
        + " ORDER BY id ASC LIMIT ? OFFSET ?"
    )
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def create_blast_job(
    *,
    campaign_id: int,
    total_recipients: int,
) -> int:
    """Record a deferred-tail row for a blast that exceeded the inline cap.

    Returns the new ``newsletter_blast_jobs.id``. The row starts at
    ``status='pending'`` / ``processed_recipients=0`` and is picked up
    by the tick worker on the next cron pulse.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO newsletter_blast_jobs "
            "(campaign_id, status, total_recipients, processed_recipients, "
            " created_at) VALUES (?, 'pending', ?, 0, ?)",
            (int(campaign_id), int(total_recipients), now),
        )
        return int(cur.lastrowid)


def fetch_next_pending_blast_job() -> Optional[dict]:
    """Return the oldest pending or running blast job, or None.

    Running rows are returned alongside pending rows so a tick that
    crashes mid-batch resumes on the next pulse rather than wedging.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT id, campaign_id, status, total_recipients, "
            " processed_recipients, created_at, started_at, finished_at "
            "FROM newsletter_blast_jobs "
            "WHERE status IN ('pending', 'running') "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def mark_blast_job_started(job_id: int) -> None:
    """Flip a blast job from ``pending`` to ``running`` and stamp
    ``started_at`` (if it isn't already set — re-entrant ticks must
    not clobber the original start time).
    """
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE newsletter_blast_jobs "
            "SET status = 'running', "
            "    started_at = COALESCE(started_at, ?) "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (now, int(job_id)),
        )


def advance_blast_job_progress(job_id: int, batch_size: int) -> dict:
    """Bump ``processed_recipients`` by *batch_size* and return the row.

    If ``processed_recipients >= total_recipients`` after the bump, the
    row flips to ``status='done'`` and ``finished_at`` is stamped.
    """
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE newsletter_blast_jobs "
            "SET processed_recipients = processed_recipients + ? "
            "WHERE id = ?",
            (int(batch_size), int(job_id)),
        )
        row = c.execute(
            "SELECT id, campaign_id, status, total_recipients, "
            " processed_recipients, created_at, started_at, finished_at "
            "FROM newsletter_blast_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if row is None:
            return {}
        if int(row["processed_recipients"]) >= int(row["total_recipients"]):
            c.execute(
                "UPDATE newsletter_blast_jobs "
                "SET status = 'done', finished_at = ? "
                "WHERE id = ?",
                (now, int(job_id)),
            )
            # Refresh after the close.
            row = c.execute(
                "SELECT id, campaign_id, status, total_recipients, "
                " processed_recipients, created_at, started_at, "
                " finished_at FROM newsletter_blast_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
    return dict(row) if row else {}


def mark_blast_job_failed(job_id: int) -> None:
    """Flip a blast job to ``failed`` and stamp ``finished_at``.

    Used by the tick worker when a batch raises hard. Pending recipients
    are abandoned — the admin can re-blast the same campaign manually
    after triaging.
    """
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE newsletter_blast_jobs "
            "SET status = 'failed', finished_at = ? "
            "WHERE id = ? AND status IN ('pending', 'running')",
            (now, int(job_id)),
        )


# ── Cursor-pagination + atomic claim (migration 194) ────────────────────────
#
# AUDIT 2026-05-15 — the original tick worker used LIMIT/OFFSET and a
# two-step fetch + ``mark_blast_job_started``. Both pieces are racy:
#
#   * OFFSET shifts when subscribers unsubscribe mid-blast — the next
#     page either resends earlier rows or skips later ones.
#   * Two scheduler instances can race the same row: both
#     ``fetch_next_pending_blast_job`` return the same job, both call
#     ``mark_blast_job_started``, both enqueue the same batch. Result:
#     duplicate emails, double-spend on the provider quota.
#
# The cursor design fixes both:
#
#   * ``claim_blast_job`` does an atomic UPDATE ... RETURNING with a
#     per-worker claim token. Only one worker wins.
#   * ``get_blast_recipients_after`` paginates by ``id > last_recipient_id``.
#     Stable across any concurrent mutation.
#   * ``advance_blast_job_progress_with_cursor`` bumps ``processed`` AND
#     ``last_recipient_id`` AND re-clears ``claim_token`` in one UPDATE.


# Claim grace window. If a worker crashed mid-batch, its claim is
# reclaimable after this many seconds. Long enough for a 500-row batch
# to drain (each enqueue is ~50 ms in the email queue), short enough
# that a real crash unblocks the queue inside the same admin polling
# interval (60 s tick → 5 min grace = a 5-min visible stall, max).
CLAIM_TTL_SECONDS = 300


def claim_blast_job(claim_token: str) -> Optional[dict]:
    """Atomically claim the next blast job for this worker.

    Uses ``UPDATE ... RETURNING`` so the fetch + the status flip happen
    inside one transaction. A second concurrent worker calling with its
    own ``claim_token`` will see an empty result and back off.

    Reclaim rule: a row whose ``claim_token`` was set more than
    ``CLAIM_TTL_SECONDS`` ago counts as abandoned and can be taken over.
    The ``started_at`` column is reused as the claim timestamp — it's
    bumped to ``now`` on every successful claim.

    Returns the claimed row (with cursor + status), or None when no
    job is available right now.
    """
    if not claim_token or not isinstance(claim_token, str):
        raise ValueError("claim_blast_job: claim_token must be a non-empty string")
    now = int(time.time())
    cutoff = now - CLAIM_TTL_SECONDS
    with db.conn() as c:
        # The atomic claim. RETURNING is required so we can read the
        # row WITHOUT a second SELECT (which would lose the claim
        # exclusivity guarantee). SQLite 3.35+ supports RETURNING.
        cur = c.execute(
            "UPDATE newsletter_blast_jobs "
            "SET status = 'running', "
            "    started_at = ?, "
            "    claim_token = ? "
            "WHERE id = ("
            "    SELECT id FROM newsletter_blast_jobs "
            "    WHERE status IN ('pending', 'running') "
            "      AND (claim_token IS NULL "
            "           OR claim_token = ? "
            "           OR COALESCE(started_at, 0) < ?) "
            "    ORDER BY id ASC LIMIT 1"
            ") "
            "RETURNING id, campaign_id, status, total_recipients, "
            "          processed_recipients, created_at, started_at, "
            "          finished_at, "
            "          COALESCE(last_recipient_id, 0) AS last_recipient_id, "
            "          claim_token",
            (now, claim_token, claim_token, cutoff),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_blast_recipients_after(
    segment: str,
    frequency_filter: Optional[str],
    *,
    last_id: int,
    limit: int,
) -> list[dict]:
    """Cursor-paginated recipient page. Uses ``WHERE id > last_id`` so
    inserts / unsubscribes between ticks don't shift the cursor.

    Same filter semantics as ``get_blast_recipients`` / ``_page``. Always
    sorted by id ASC so callers can use ``last_recipient_id`` as the
    monotonic cursor.
    """
    seg = segment if segment in VALID_SEGMENTS else "all"
    where = [
        "confirmed_at IS NOT NULL",
        "unsubscribed_at IS NULL",
        "id > ?",
    ]
    params: list = [int(last_id)]

    if seg != "all":
        where.append("(segment = ? OR segment = 'all')")
        params.append(seg)
    if frequency_filter and frequency_filter in VALID_FREQUENCIES:
        where.append("frequency = ?")
        params.append(frequency_filter)

    safe_limit = min(max(1, int(limit)), 5_000)
    params.append(safe_limit)

    sql = (
        "SELECT id, email, segment, frequency FROM newsletter_subscribers "
        "WHERE " + " AND ".join(where)
        + " ORDER BY id ASC LIMIT ?"
    )
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def advance_blast_job_progress_with_cursor(
    job_id: int,
    *,
    batch_size: int,
    last_recipient_id: int,
    claim_token: str,
) -> dict:
    """Bump processed + last_recipient_id and release the claim atomically.

    Returns the post-update row. If the row's ``claim_token`` doesn't
    match the caller's, the UPDATE no-ops — a crashed-then-reclaimed
    worker can't clobber the surviving worker's progress.

    Hits ``processed_recipients >= total_recipients`` → flips status to
    ``done`` and stamps ``finished_at``. Otherwise releases the claim
    (claim_token = NULL) so the next tick can pick the row up cleanly.
    """
    if not claim_token:
        raise ValueError(
            "advance_blast_job_progress_with_cursor: claim_token required",
        )
    now = int(time.time())
    with db.conn() as c:
        # Bump processed + cursor, ONLY if the claim is still ours.
        c.execute(
            "UPDATE newsletter_blast_jobs "
            "SET processed_recipients = processed_recipients + ?, "
            "    last_recipient_id = MAX(COALESCE(last_recipient_id, 0), ?), "
            "    claim_token = NULL "
            "WHERE id = ? AND claim_token = ?",
            (int(batch_size), int(last_recipient_id), int(job_id), claim_token),
        )
        row = c.execute(
            "SELECT id, campaign_id, status, total_recipients, "
            " processed_recipients, created_at, started_at, finished_at, "
            " COALESCE(last_recipient_id, 0) AS last_recipient_id "
            "FROM newsletter_blast_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
        if row is None:
            return {}
        if int(row["processed_recipients"]) >= int(row["total_recipients"]):
            c.execute(
                "UPDATE newsletter_blast_jobs "
                "SET status = 'done', finished_at = ? "
                "WHERE id = ?",
                (now, int(job_id)),
            )
            row = c.execute(
                "SELECT id, campaign_id, status, total_recipients, "
                " processed_recipients, created_at, started_at, "
                " finished_at, "
                " COALESCE(last_recipient_id, 0) AS last_recipient_id "
                "FROM newsletter_blast_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
    return dict(row) if row else {}


def get_blast_job(job_id: int) -> Optional[dict]:
    """Look up a single blast job by id."""
    with db.conn() as c:
        row = c.execute(
            "SELECT id, campaign_id, status, total_recipients, "
            " processed_recipients, created_at, started_at, finished_at "
            "FROM newsletter_blast_jobs WHERE id = ?",
            (int(job_id),),
        ).fetchone()
    return dict(row) if row else None


def get_blast_job_for_campaign(campaign_id: int) -> Optional[dict]:
    """Return the deferred-tail job for *campaign_id* if one exists."""
    with db.conn() as c:
        row = c.execute(
            "SELECT id, campaign_id, status, total_recipients, "
            " processed_recipients, created_at, started_at, finished_at "
            "FROM newsletter_blast_jobs "
            "WHERE campaign_id = ? ORDER BY id DESC LIMIT 1",
            (int(campaign_id),),
        ).fetchone()
    return dict(row) if row else None


def backfill_campaign_sent_at(campaign_id: int, sent_at: int) -> None:
    """Set ``newsletter_campaigns.sent_at`` once the deferred tail
    finishes. The "sent now" handler only sets ``sent_at`` for the
    inline portion; the tail completion stamps the campaign as fully
    sent.
    """
    with db.conn() as c:
        c.execute(
            "UPDATE newsletter_campaigns SET sent_at = ? "
            "WHERE id = ? AND sent_at IS NULL",
            (int(sent_at), int(campaign_id)),
        )


__all__ = [
    'subscribe_newsletter',
    'get_newsletter_position',
    'confirm_newsletter',
    'unsubscribe_newsletter',
    'list_newsletter_campaigns',
    'count_blast_recipients',
    'get_blast_recipients',
    'get_blast_recipients_page',
    'get_blast_recipients_after',
    'record_newsletter_campaign',
    'create_blast_job',
    'fetch_next_pending_blast_job',
    'claim_blast_job',
    'mark_blast_job_started',
    'advance_blast_job_progress',
    'advance_blast_job_progress_with_cursor',
    'mark_blast_job_failed',
    'get_blast_job',
    'get_blast_job_for_campaign',
    'backfill_campaign_sent_at',
    'MAX_INLINE_RECIPIENTS',
    'MAX_BATCH_PER_TICK',
    'CLAIM_TTL_SECONDS',
    'VALID_SEGMENTS',
    'VALID_FREQUENCIES',
    'CONFIRMATION_RESEND_COOLDOWN_S',
]

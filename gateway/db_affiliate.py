"""Affiliate-program DB helpers.

Kept in a separate module from ``db.py`` so the affiliate feature can be
added/removed without touching the high-traffic main DB file. Uses
``db.conn()`` for connections so it picks up the same thread-local,
WAL-enabled sqlite handle as every other caller.

See ``migrations/033_affiliate_program.py`` for the schema. One
AffiliateAccount per user; links + conversions cascade delete with the
account. Money is stored in integer pence end-to-end — convert to GBP
at the rendering edge.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Optional

import db


AFFILIATE_TIERS = ("partner", "premium_partner", "top_tier")
MIN_COMMISSION_RATE = 0.05  # spec says 10-40%; we accept 5-50% for headroom
MAX_COMMISSION_RATE = 0.50
DEFAULT_PAYOUT_THRESHOLD_PENCE = 5000  # £50 per spec
AFFILIATE_COOKIE_NAME = "affiliate_code"
AFFILIATE_COOKIE_MAX_AGE_SECONDS = 90 * 86400  # 90 days
AFFILIATE_ATTRIBUTION_WINDOW_SECONDS = 90 * 86400


def _new_affiliate_code() -> str:
    """10-char URL-safe code. Disjoint from newsletter referral_code (8 char)
    so there's no chance of cross-lookup confusion if one leaks into the
    other namespace.
    """
    return secrets.token_urlsafe(8)[:10]


# ── Account management ────────────────────────────────────────────────


def create_affiliate_account(
    user_id: int,
    *,
    commission_rate: float,
    tier: str,
    approved_by_admin_id: Optional[int],
    payout_method: Optional[str] = None,
    payout_email: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Create an AffiliateAccount for an existing user.

    Raises ValueError on bad inputs (commission_rate outside valid band,
    unknown tier, user already has an affiliate account). Caller is
    responsible for checking admin permission before calling this.
    """
    if not (MIN_COMMISSION_RATE <= float(commission_rate) <= MAX_COMMISSION_RATE):
        raise ValueError(
            f"commission_rate must be between {MIN_COMMISSION_RATE} and {MAX_COMMISSION_RATE}"
        )
    if tier not in AFFILIATE_TIERS:
        raise ValueError(f"tier must be one of {AFFILIATE_TIERS}")

    now = int(time.time())
    with db.conn() as c:
        # Friendlier error than the UNIQUE(user_id) constraint violation.
        dup = c.execute(
            "SELECT id FROM affiliate_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if dup:
            raise ValueError(
                f"user {user_id} already has affiliate account id={dup['id']}"
            )

        # Retry loop for the rare code collision. secrets.token_urlsafe(8)
        # gives ~60 bits of entropy — collisions are astronomically unlikely
        # at realistic scale, but we retry a few times to be safe.
        last_err: Optional[Exception] = None
        for _ in range(5):
            code = _new_affiliate_code()
            try:
                cur = c.execute(
                    "INSERT INTO affiliate_accounts ("
                    " user_id, affiliate_code, commission_rate, tier,"
                    " approved_by_admin_id, approved_at, is_active,"
                    " payout_method, payout_email, notes,"
                    " created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        user_id, code, float(commission_rate), tier,
                        approved_by_admin_id, now,
                        payout_method, payout_email, notes,
                        now, now,
                    ),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError as e:
                # UNIQUE(affiliate_code) collision → try a fresh code.
                last_err = e
                continue
        raise RuntimeError(
            f"affiliate code generation failed after 5 retries: {last_err}"
        )


def get_affiliate_by_id(affiliate_id: int) -> Optional[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_accounts WHERE id = ?", (affiliate_id,)
        ).fetchone()


def get_affiliate_by_user_id(user_id: int) -> Optional[sqlite3.Row]:
    """Return the user's affiliate account or None. Does NOT filter on
    is_active — callers decide whether a deactivated account should be
    hidden from the affiliate dashboard (it should) or visible in admin."""
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_affiliate_by_code(code: str) -> Optional[sqlite3.Row]:
    """Look up by the opaque public affiliate_code. Deactivated accounts
    return the row — the ``/partner/{code}`` handler treats inactive rows
    as invalid and redirects without setting a cookie, so checking
    ``is_active`` is the caller's responsibility."""
    if not code:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_accounts WHERE affiliate_code = ?",
            (code.strip(),),
        ).fetchone()


def list_affiliates(include_inactive: bool = True) -> list[sqlite3.Row]:
    """Return all affiliate accounts joined with the owning user's email.

    Ordered by total_earnings_pence desc so the admin table surfaces the
    top performers first.
    """
    sql = (
        "SELECT aa.*, u.email AS user_email, u.username AS user_username "
        "FROM affiliate_accounts aa "
        "JOIN users u ON u.id = aa.user_id "
    )
    if not include_inactive:
        sql += "WHERE aa.is_active = 1 "
    sql += "ORDER BY aa.total_earnings_pence DESC, aa.created_at DESC"
    with db.conn() as c:
        return c.execute(sql).fetchall()


def update_affiliate_account(
    affiliate_id: int,
    *,
    commission_rate: Optional[float] = None,
    tier: Optional[str] = None,
    is_active: Optional[bool] = None,
    payout_method: Optional[str] = None,
    payout_email: Optional[str] = None,
    notes: Optional[str] = None,
) -> bool:
    """Partial update; only non-None fields are written. Returns True on
    successful update, False if the account didn't exist.
    """
    fields: list[str] = []
    values: list = []
    if commission_rate is not None:
        if not (MIN_COMMISSION_RATE <= float(commission_rate) <= MAX_COMMISSION_RATE):
            raise ValueError(
                f"commission_rate must be between {MIN_COMMISSION_RATE} and {MAX_COMMISSION_RATE}"
            )
        fields.append("commission_rate = ?")
        values.append(float(commission_rate))
    if tier is not None:
        if tier not in AFFILIATE_TIERS:
            raise ValueError(f"tier must be one of {AFFILIATE_TIERS}")
        fields.append("tier = ?")
        values.append(tier)
    if is_active is not None:
        fields.append("is_active = ?")
        values.append(1 if is_active else 0)
    if payout_method is not None:
        fields.append("payout_method = ?")
        values.append(payout_method)
    if payout_email is not None:
        fields.append("payout_email = ?")
        values.append(payout_email)
    if notes is not None:
        fields.append("notes = ?")
        values.append(notes)
    if not fields:
        return False

    fields.append("updated_at = ?")
    values.append(int(time.time()))
    values.append(affiliate_id)

    with db.conn() as c:
        cur = c.execute(
            f"UPDATE affiliate_accounts SET {', '.join(fields)} WHERE id = ?",
            tuple(values),
        )
    return cur.rowcount > 0


# ── Affiliate links ────────────────────────────────────────────────────


def normalise_utm_slug(raw: str) -> str:
    """Lowercase, strip, keep only [a-z0-9_-], max 40 chars.

    Empty string if nothing remains after filtering. Kept strict —
    affiliates typing mixed case / whitespace / unicode get the same
    output so the UNIQUE(account, campaign) index collapses duplicates.
    """
    if not raw:
        return ""
    cleaned = "".join(
        ch for ch in raw.strip().lower()
        if ch.isalnum() or ch in ("_", "-")
    )
    return cleaned[:40]


def create_affiliate_link(
    affiliate_account_id: int,
    utm_campaign: str,
    utm_content: Optional[str] = None,
) -> int:
    """Create a tracking link. utm_campaign is unique per account; if one
    already exists with the same slug, returns its id (idempotent-ish).
    """
    slug = normalise_utm_slug(utm_campaign)
    if not slug:
        raise ValueError("utm_campaign must be 1-40 chars of [a-z0-9_-]")

    now = int(time.time())
    with db.conn() as c:
        existing = c.execute(
            "SELECT id FROM affiliate_links "
            "WHERE affiliate_account_id = ? AND utm_campaign = ?",
            (affiliate_account_id, slug),
        ).fetchone()
        if existing:
            return existing["id"]
        cur = c.execute(
            "INSERT INTO affiliate_links "
            "(affiliate_account_id, utm_campaign, utm_content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (affiliate_account_id, slug, utm_content, now),
        )
        return cur.lastrowid


def list_affiliate_links(affiliate_account_id: int) -> list[sqlite3.Row]:
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_links "
            "WHERE affiliate_account_id = ? "
            "ORDER BY created_at DESC",
            (affiliate_account_id,),
        ).fetchall()


def get_affiliate_link_by_campaign(
    affiliate_account_id: int, utm_campaign: str
) -> Optional[sqlite3.Row]:
    slug = normalise_utm_slug(utm_campaign)
    if not slug:
        return None
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_links "
            "WHERE affiliate_account_id = ? AND utm_campaign = ?",
            (affiliate_account_id, slug),
        ).fetchone()


# ── Click / signup / conversion state transitions ────────────────────


def record_affiliate_click(
    affiliate_account_id: int,
    link_id: Optional[int] = None,
    click_fingerprint: Optional[str] = None,
) -> int:
    """Insert a fresh conversion row with only clicked_at populated.
    Returns the new conversion id. Also bumps affiliate_links.clicks
    if link_id is provided.

    The affiliate_code cookie alone is enough for signup-time lookup —
    we don't stuff the conversion id into the cookie because that would
    require signing to be safe.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO affiliate_conversions ("
            " affiliate_account_id, affiliate_link_id,"
            " click_fingerprint, clicked_at"
            ") VALUES (?, ?, ?, ?)",
            (affiliate_account_id, link_id, click_fingerprint, now),
        )
        conv_id = cur.lastrowid
        if link_id is not None:
            c.execute(
                "UPDATE affiliate_links SET clicks = clicks + 1 WHERE id = ?",
                (link_id,),
            )
    return conv_id


def get_affiliate_conversion_for_user(
    user_id: int,
) -> Optional[sqlite3.Row]:
    """Return the conversion row attributing this user to an affiliate,
    if any. Used by the Stripe webhook (when wired) to find the row to
    update when the user makes their first payment.
    """
    with db.conn() as c:
        return c.execute(
            "SELECT * FROM affiliate_conversions "
            "WHERE referred_user_id = ? "
            "ORDER BY signed_up_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()


def attach_signup_to_affiliate(
    affiliate_account_id: int,
    user_id: int,
    *,
    fallback_fingerprint: Optional[str] = None,
) -> Optional[int]:
    """Called from /auth/register when the affiliate_code cookie is set.

    Strategy:
      1. If the user already has any affiliate conversion row, do nothing
         (idempotent re-attribution guard — prevents a user from clicking
         a second affiliate's link and pulling credit from the first).
      2. Otherwise, claim the most recent unclaimed click row for this
         affiliate within the 90-day attribution window. Bumps the link's
         ``conversions`` counter if a link was attached at click time.
      3. If no unclaimed click row exists (cookie survived a DB wipe or
         the click was never recorded), create a fresh conversion row
         with signed_up_at populated and ``source_note`` = "cookie_without_click".

    Returns the conversion id we wrote, or None if step 1 hit.
    """
    now = int(time.time())
    window_start = now - AFFILIATE_ATTRIBUTION_WINDOW_SECONDS

    with db.conn() as c:
        # Step 1: re-attribution guard
        already = c.execute(
            "SELECT id FROM affiliate_conversions WHERE referred_user_id = ?",
            (user_id,),
        ).fetchone()
        if already:
            return None

        # Step 2: claim the most recent unclaimed click
        claimable = c.execute(
            "SELECT id, affiliate_link_id FROM affiliate_conversions "
            "WHERE affiliate_account_id = ? "
            "  AND referred_user_id IS NULL "
            "  AND signed_up_at IS NULL "
            "  AND clicked_at >= ? "
            "ORDER BY clicked_at DESC LIMIT 1",
            (affiliate_account_id, window_start),
        ).fetchone()

        if claimable:
            c.execute(
                "UPDATE affiliate_conversions "
                "SET referred_user_id = ?, signed_up_at = ? WHERE id = ?",
                (user_id, now, claimable["id"]),
            )
            conv_id = claimable["id"]
            link_id = claimable["affiliate_link_id"]
        else:
            # Step 3: cookie without a matching click.
            cur = c.execute(
                "INSERT INTO affiliate_conversions ("
                " affiliate_account_id, referred_user_id,"
                " click_fingerprint, clicked_at, signed_up_at,"
                " source_note"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    affiliate_account_id, user_id,
                    fallback_fingerprint, now, now,
                    "cookie_without_click",
                ),
            )
            conv_id = cur.lastrowid
            link_id = None

        # Bump the link conversion counter if we claimed a link-attributed
        # click. "Conversion" at the link level means signup; first-payment
        # is tracked separately via ``converted_at``.
        if link_id is not None:
            c.execute(
                "UPDATE affiliate_links "
                "SET conversions = conversions + 1 WHERE id = ?",
                (link_id,),
            )

        # Denormalized total on the account for the admin list.
        c.execute(
            "UPDATE affiliate_accounts "
            "SET total_conversions = total_conversions + 1, updated_at = ? "
            "WHERE id = ?",
            (now, affiliate_account_id),
        )

    return conv_id


def mark_affiliate_conversion_paid(
    conversion_id: int,
    first_payment_amount_pence: int,
) -> bool:
    """Flip a signed-up conversion into a paid one. Called from the
    Stripe webhook handler (not yet wired) when
    ``checkout.session.completed`` fires for a user we can trace back
    to an affiliate conversion via ``get_affiliate_conversion_for_user``.

    Leaves ``commission_amount_pence`` NULL so the commission-calc job
    picks it up on its next run. Keeps the two writes separate so a
    failure in commission calc doesn't lose the payment fact.

    Returns True if the row was updated. Idempotent — double-firing the
    webhook is a no-op because we guard with ``converted_at IS NULL``.
    """
    now = int(time.time())
    with db.conn() as c:
        cur = c.execute(
            "UPDATE affiliate_conversions "
            "SET converted_at = ?, first_payment_amount_pence = ? "
            "WHERE id = ? AND converted_at IS NULL",
            (now, int(first_payment_amount_pence), conversion_id),
        )
    return cur.rowcount > 0


def list_conversions_awaiting_commission_calc(
    limit: int = 200,
) -> list[sqlite3.Row]:
    """Rows with a first payment recorded but no commission yet. The
    daily job walks this list.
    """
    with db.conn() as c:
        return c.execute(
            "SELECT c.*, a.commission_rate, a.user_id AS affiliate_user_id "
            "FROM affiliate_conversions c "
            "JOIN affiliate_accounts a ON a.id = c.affiliate_account_id "
            "WHERE c.converted_at IS NOT NULL "
            "  AND c.commission_amount_pence IS NULL "
            "  AND c.first_payment_amount_pence IS NOT NULL "
            "ORDER BY c.converted_at ASC LIMIT ?",
            (limit,),
        ).fetchall()


def record_commission_calculated(
    conversion_id: int,
    commission_amount_pence: int,
) -> bool:
    """Stamp the commission and bump the account's total_earnings_pence
    counter atomically. Returns True if the row was updated.

    Guarded against re-running: the UPDATE includes
    ``commission_amount_pence IS NULL`` so a re-queued job can't
    double-count earnings.
    """
    amt = int(commission_amount_pence)
    with db.conn() as c:
        cur = c.execute(
            "UPDATE affiliate_conversions "
            "SET commission_amount_pence = ? "
            "WHERE id = ? AND commission_amount_pence IS NULL",
            (amt, conversion_id),
        )
        if cur.rowcount == 0:
            return False
        c.execute(
            "UPDATE affiliate_accounts "
            "SET total_earnings_pence = total_earnings_pence + ?, updated_at = ? "
            "WHERE id = (SELECT affiliate_account_id "
            "            FROM affiliate_conversions WHERE id = ?)",
            (amt, int(time.time()), conversion_id),
        )
    return True


def list_affiliate_conversions(
    affiliate_account_id: int,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Conversion rows for one affiliate's dashboard. Joined with the
    referred user's email (the route layer anonymises it before rendering).
    Only includes rows where signup has happened.
    """
    with db.conn() as c:
        return c.execute(
            "SELECT c.*, u.email AS referred_email, u.username AS referred_username "
            "FROM affiliate_conversions c "
            "LEFT JOIN users u ON u.id = c.referred_user_id "
            "WHERE c.affiliate_account_id = ? "
            "  AND c.signed_up_at IS NOT NULL "
            "ORDER BY c.signed_up_at DESC LIMIT ?",
            (affiliate_account_id, limit),
        ).fetchall()


def sum_affiliate_commissions(
    affiliate_account_id: int,
) -> dict:
    """Return summary stats for the affiliate dashboard header:
    ``{earned_pence, paid_pence, pending_pence, conversion_count,
    paid_conversion_count, click_count}``.
    """
    with db.conn() as c:
        row = c.execute(
            "SELECT "
            " COALESCE(SUM(CASE WHEN commission_amount_pence IS NOT NULL "
            "                    THEN commission_amount_pence ELSE 0 END), 0) "
            "   AS earned, "
            " COALESCE(SUM(CASE WHEN commission_paid = 1 "
            "                    THEN commission_amount_pence ELSE 0 END), 0) "
            "   AS paid, "
            " COUNT(CASE WHEN signed_up_at IS NOT NULL THEN 1 END) "
            "   AS conversion_count, "
            " COUNT(CASE WHEN converted_at IS NOT NULL THEN 1 END) "
            "   AS paid_conversion_count, "
            " COUNT(*) AS click_count "
            "FROM affiliate_conversions WHERE affiliate_account_id = ?",
            (affiliate_account_id,),
        ).fetchone()
    earned = int(row["earned"] or 0)
    paid = int(row["paid"] or 0)
    return {
        "earned_pence": earned,
        "paid_pence": paid,
        "pending_pence": earned - paid,
        "conversion_count": int(row["conversion_count"] or 0),
        "paid_conversion_count": int(row["paid_conversion_count"] or 0),
        "click_count": int(row["click_count"] or 0),
    }


def list_affiliate_pending_payouts(
    min_pence: int = DEFAULT_PAYOUT_THRESHOLD_PENCE,
) -> list[dict]:
    """Every active affiliate with pending commission >= ``min_pence``.

    Returned as plain dicts (not Row objects) because the admin payout
    view combines data from multiple tables and mutates it for rendering.
    """
    with db.conn() as c:
        rows = c.execute(
            "SELECT a.id AS affiliate_id, a.user_id, a.tier, "
            "       a.payout_method, a.payout_email, a.commission_rate, "
            "       u.email AS user_email, u.username AS user_username, "
            "       COALESCE(SUM(CASE WHEN c.commission_paid = 0 "
            "                         THEN c.commission_amount_pence ELSE 0 END), 0) "
            "         AS pending_pence, "
            "       COUNT(CASE WHEN c.commission_paid = 0 "
            "                   AND c.commission_amount_pence IS NOT NULL "
            "                  THEN 1 END) AS unpaid_count "
            "FROM affiliate_accounts a "
            "JOIN users u ON u.id = a.user_id "
            "LEFT JOIN affiliate_conversions c ON c.affiliate_account_id = a.id "
            "WHERE a.is_active = 1 "
            "GROUP BY a.id "
            "HAVING pending_pence >= ? "
            "ORDER BY pending_pence DESC",
            (int(min_pence),),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_affiliate_payout_complete(
    affiliate_account_id: int,
    admin_id: int,
) -> dict:
    """Flip every unpaid-but-calculated conversion for this affiliate to
    commission_paid = 1. Returns summary ``{"rows": n, "total_paid_pence": sum}``.

    Caller should only invoke this after confirming out-of-band that the
    payment actually went out (PayPal / wire / etc.). The DB has no
    concept of rolling back the flip.
    """
    now = int(time.time())
    with db.conn() as c:
        # Snapshot the sum before updating so we can return it atomically.
        snapshot = c.execute(
            "SELECT COALESCE(SUM(commission_amount_pence), 0) AS total, "
            "       COUNT(*) AS rows_count "
            "FROM affiliate_conversions "
            "WHERE affiliate_account_id = ? "
            "  AND commission_paid = 0 "
            "  AND commission_amount_pence IS NOT NULL",
            (affiliate_account_id,),
        ).fetchone()
        total_pence = int(snapshot["total"] or 0)
        rows_count = int(snapshot["rows_count"] or 0)
        if rows_count == 0:
            return {"rows": 0, "total_paid_pence": 0}

        c.execute(
            "UPDATE affiliate_conversions "
            "SET commission_paid = 1, commission_paid_at = ?, "
            "    commission_paid_by_admin_id = ? "
            "WHERE affiliate_account_id = ? "
            "  AND commission_paid = 0 "
            "  AND commission_amount_pence IS NOT NULL",
            (now, admin_id, affiliate_account_id),
        )
        c.execute(
            "UPDATE affiliate_accounts SET updated_at = ? WHERE id = ?",
            (now, affiliate_account_id),
        )
    return {"rows": rows_count, "total_paid_pence": total_pence}


def anonymise_email(email: Optional[str]) -> str:
    """Turn jake@example.com → jake@.com for display on the affiliate
    dashboard (per spec). Falls back to "—" if no email or malformed.

    Kept simple: split once on @, keep everything before it, append ".com"
    stub. The goal is to tell the affiliate WHICH of their conversions
    corresponds to a real user without leaking the specific domain.
    """
    if not email or "@" not in email:
        return "—"
    local = email.split("@", 1)[0]
    if not local:
        return "—"
    return f"{local}@.com"

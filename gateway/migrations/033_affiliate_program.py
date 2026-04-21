"""Private affiliate program — admin-invited partners only.

Distinct from the public referral program in ``021_referrals_leaderboard``.
Affiliates are:
  - Created by admins only (no public application form).
  - Tied to an existing user account (one AffiliateAccount per user).
  - Paid higher commission rates than public referrals (10–40%).
  - Tracked via a separate ``affiliate_code`` cookie (90-day attribution).

Three tables:
  - ``affiliate_accounts``  one row per trusted partner, stores tier,
    commission rate, payout config, cached totals.
  - ``affiliate_conversions`` one row per click that led to a signup; gets
    populated progressively as the user signs up → pays → commission paid.
  - ``affiliate_links`` custom tracking links (e.g., per podcast episode)
    that share the same parent ``affiliate_code`` but carry their own
    ``utm_campaign`` + click/conversion counters.

Money columns use pence (INTEGER) to match the rest of the codebase
(``monthly_cents``, gift subscription config). The spec lists
``total_earnings_gbp: float`` on the account; we store pence under
``total_earnings_pence`` and the UI/JSON converts to GBP at render time.

All columns nullable or default-safe → pure additive migration, no
backfill, no downstream breakage.
"""

revision = "033"
down_revision = "032"


def upgrade(c):
    # ── affiliate_accounts ─────────────────────────────────────────────
    # One row per approved partner. UNIQUE(user_id) means a single user
    # can only hold one affiliate account; re-activate rather than
    # creating a duplicate.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS affiliate_accounts (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                INTEGER NOT NULL UNIQUE
                                   REFERENCES users(id) ON DELETE CASCADE,
            affiliate_code         TEXT NOT NULL UNIQUE,
            commission_rate        REAL NOT NULL DEFAULT 0.20,
            tier                   TEXT NOT NULL DEFAULT 'partner',
            approved_by_admin_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
            approved_at            INTEGER NOT NULL,
            is_active              INTEGER NOT NULL DEFAULT 1,
            total_conversions      INTEGER NOT NULL DEFAULT 0,
            total_earnings_pence   INTEGER NOT NULL DEFAULT 0,
            payout_method          TEXT,
            payout_email           TEXT,
            notes                  TEXT,
            created_at             INTEGER NOT NULL,
            updated_at             INTEGER NOT NULL
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_affiliate_accounts_active "
        "ON affiliate_accounts(is_active, tier)"
    )

    # ── affiliate_links ────────────────────────────────────────────────
    # Per-campaign tracking links owned by one affiliate. Represented as
    # ``/p/<affiliate_code>?c=<utm_campaign>`` in the UI. Click / conv
    # counters are maintained by the public-facing endpoints.
    # Declared BEFORE affiliate_conversions so the FK reference below is
    # not a forward reference (SQLite would accept it, but explicit
    # ordering is clearer and survives future FK-enforcement flips).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS affiliate_links (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_account_id  INTEGER NOT NULL
                                  REFERENCES affiliate_accounts(id)
                                  ON DELETE CASCADE,
            utm_campaign          TEXT NOT NULL,
            utm_content           TEXT,
            clicks                INTEGER NOT NULL DEFAULT 0,
            conversions           INTEGER NOT NULL DEFAULT 0,
            created_at            INTEGER NOT NULL,
            UNIQUE(affiliate_account_id, utm_campaign)
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_aff_links_account "
        "ON affiliate_links(affiliate_account_id)"
    )

    # ── affiliate_conversions ──────────────────────────────────────────
    # Progressive-state row. On click → (clicked_at) only. On signup →
    # (signed_up_at, referred_user_id). On paid → (converted_at,
    # first_payment_amount_pence, commission_amount_pence). On payout →
    # (commission_paid, commission_paid_at).
    #
    # ``affiliate_link_id`` is nullable: clicks via the default partner
    # URL won't have a per-campaign link id. Anonymous clicks (no user
    # yet) get ``referred_user_id`` filled in later on signup.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS affiliate_conversions (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            affiliate_account_id        INTEGER NOT NULL
                                        REFERENCES affiliate_accounts(id)
                                        ON DELETE CASCADE,
            affiliate_link_id           INTEGER
                                        REFERENCES affiliate_links(id)
                                        ON DELETE SET NULL,
            referred_user_id            INTEGER
                                        REFERENCES users(id) ON DELETE SET NULL,
            click_fingerprint           TEXT,
            clicked_at                  INTEGER NOT NULL,
            signed_up_at                INTEGER,
            converted_at                INTEGER,
            first_payment_amount_pence  INTEGER,
            commission_amount_pence     INTEGER,
            commission_paid             INTEGER NOT NULL DEFAULT 0,
            commission_paid_at          INTEGER,
            commission_paid_by_admin_id INTEGER
                                        REFERENCES users(id) ON DELETE SET NULL,
            source_note                 TEXT
        )
        """
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_aff_conv_account "
        "ON affiliate_conversions(affiliate_account_id, clicked_at DESC)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_aff_conv_referred "
        "ON affiliate_conversions(referred_user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_aff_conv_pending_commission "
        "ON affiliate_conversions("
        "affiliate_account_id, commission_paid, commission_amount_pence"
        ")"
    )
    # For the commission-calc job: rows that have converted but not yet
    # had commission calculated. Filtering on ``commission_amount_pence
    # IS NULL`` in a partial index.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_aff_conv_await_calc "
        "ON affiliate_conversions(converted_at) "
        "WHERE commission_amount_pence IS NULL AND converted_at IS NOT NULL"
    )


def downgrade(c):
    # Drop children before parents so FK references are resolved cleanly
    # even if PRAGMA foreign_keys=ON.
    c.execute("DROP TABLE IF EXISTS affiliate_conversions")
    c.execute("DROP TABLE IF EXISTS affiliate_links")
    c.execute("DROP TABLE IF EXISTS affiliate_accounts")

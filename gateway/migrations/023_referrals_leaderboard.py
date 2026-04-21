"""Private referral program + opt-in leaderboard scaffolding.

Adds:
  - `users` columns: referral_code (stable public handle), referred_by_user_id,
    referral_credits_earned_months, leaderboard_participation, leaderboard_handle.
  - `referrals` table: one row per invitee, tracks conversion + reward state.
  - `user_accuracy` table: per-user leaderboard metrics (accuracy_score,
    total_predictions, correct_predictions, last_computed_at).

All columns are nullable or have safe defaults so the migration is a pure
additive change — no rebuild, no data backfill, no downstream code breaks
until the new code-path deliberately reads them.

A note on `user_accuracy`: the gateway's predictions table is keyed on
source_handle, not user_id, so per-user scoring is not computable from the
current data. This migration still provisions the table + indexes so the
opt-in flow can ship and the scorer can populate it incrementally once a
user's `leaderboard_handle` matches a known source_handle. Users whose
handle doesn't match any source_handle will stay at accuracy_score=NULL
("Unranked") until the upstream predictions pipeline fills in.
"""

revision = "023"
down_revision = "020"


def _existing_cols(c, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c):
    # ── users additions ──────────────────────────────────────────────
    user_cols = _existing_cols(c, "users")
    if "referral_code" not in user_cols:
        # 10-char alphanumeric code; unique index below. Nullable because
        # existing rows need a backfill that we do lazily at first-read.
        c.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
    if "referred_by_user_id" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN referred_by_user_id INTEGER "
            "REFERENCES users(id) ON DELETE SET NULL"
        )
    if "referral_credits_earned_months" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN "
            "referral_credits_earned_months INTEGER NOT NULL DEFAULT 0"
        )
    if "leaderboard_participation" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN "
            "leaderboard_participation INTEGER NOT NULL DEFAULT 0"
        )
    if "leaderboard_handle" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN leaderboard_handle TEXT")

    # Unique + lookup indexes. UNIQUE is a partial index so the NULL rows
    # created by ALTER don't collide with each other before backfill.
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
        "ON users(referral_code) WHERE referral_code IS NOT NULL"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_referred_by "
        "ON users(referred_by_user_id)"
    )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_leaderboard_handle "
        "ON users(leaderboard_handle) WHERE leaderboard_handle IS NOT NULL"
    )

    # ── referrals table ──────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_user_id    INTEGER NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
            referred_user_id    INTEGER
                                REFERENCES users(id) ON DELETE CASCADE,
            referred_email      TEXT,
            invite_token_id     INTEGER
                                REFERENCES invite_tokens(id) ON DELETE SET NULL,
            created_at          INTEGER NOT NULL,
            converted_to_paid   INTEGER NOT NULL DEFAULT 0,
            converted_at        INTEGER,
            reward_granted      INTEGER NOT NULL DEFAULT 0,
            reward_granted_at   INTEGER,
            reward_type         TEXT,
            reward_months       INTEGER,
            reward_tier         TEXT,
            gifted_subscription_id INTEGER
                                REFERENCES gifted_subscriptions(id)
                                ON DELETE SET NULL
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_referrals_referrer "
        "ON referrals(referrer_user_id, converted_to_paid, reward_granted)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_referrals_referred "
        "ON referrals(referred_user_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_referrals_pending_reward "
        "ON referrals(converted_to_paid, reward_granted)"
    )

    # ── user_accuracy table ──────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_accuracy (
            user_id             INTEGER PRIMARY KEY
                                REFERENCES users(id) ON DELETE CASCADE,
            accuracy_score      REAL,
            total_predictions   INTEGER NOT NULL DEFAULT 0,
            correct_predictions INTEGER NOT NULL DEFAULT 0,
            accuracy_all_time   REAL,
            accuracy_90d        REAL,
            accuracy_30d        REAL,
            accuracy_7d         REAL,
            last_computed_at    INTEGER
        )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_accuracy_score "
        "ON user_accuracy(accuracy_score DESC)"
    )


def downgrade(c):
    # Drop new tables first (their FKs reference users columns we'll drop).
    c.execute("DROP TABLE IF EXISTS user_accuracy")
    c.execute("DROP TABLE IF EXISTS referrals")

    # Drop indexes before columns; SQLite is lenient but explicit is clearer.
    c.execute("DROP INDEX IF EXISTS idx_users_referral_code")
    c.execute("DROP INDEX IF EXISTS idx_users_referred_by")
    c.execute("DROP INDEX IF EXISTS idx_users_leaderboard_handle")

    # DROP COLUMN requires SQLite 3.35+; falls back to no-op on older.
    for col in (
        "referral_code",
        "referred_by_user_id",
        "referral_credits_earned_months",
        "leaderboard_participation",
        "leaderboard_handle",
    ):
        try:
            c.execute(f"ALTER TABLE users DROP COLUMN {col}")
        except Exception:
            pass

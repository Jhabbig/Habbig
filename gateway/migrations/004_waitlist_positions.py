"""Waitlist positions + referral codes on newsletter_subscribers."""

revision = "004"
down_revision = "003"


def upgrade(c):
    cols = {row["name"] for row in c.execute("PRAGMA table_info(newsletter_subscribers)")}
    if "position" not in cols:
        c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN position INTEGER")
    if "display_position" not in cols:
        c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN display_position INTEGER")
    if "referral_code" not in cols:
        c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN referral_code TEXT")
    if "referred_by_code" not in cols:
        c.execute("ALTER TABLE newsletter_subscribers ADD COLUMN referred_by_code TEXT")

    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_newsletter_position ON newsletter_subscribers(position) WHERE position IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_newsletter_referral ON newsletter_subscribers(referral_code) WHERE referral_code IS NOT NULL")

    # Backfill positions for any existing subscribers so they get a slot.
    import secrets
    rows = c.execute(
        "SELECT id FROM newsletter_subscribers WHERE position IS NULL ORDER BY subscribed_at ASC"
    ).fetchall()
    for i, r in enumerate(rows, start=1):
        code = secrets.token_urlsafe(6)[:8].upper().replace("-", "X").replace("_", "X")
        c.execute(
            "UPDATE newsletter_subscribers SET position = ?, display_position = ?, referral_code = ? WHERE id = ?",
            (i, i, code, r["id"]),
        )


def downgrade(c):
    pass  # additive

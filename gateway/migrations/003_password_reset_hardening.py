"""Harden password reset: store hash not plaintext, track used IP, invalidate JWTs."""

revision = "003"
down_revision = "002"


def upgrade(c):
    # Add used-from-ip + invalidated flag to password_resets. The `used` int
    # column from the original schema is kept for backwards compatibility.
    reset_cols = {row["name"] for row in c.execute("PRAGMA table_info(password_resets)")}
    if "used_from_ip" not in reset_cols:
        c.execute("ALTER TABLE password_resets ADD COLUMN used_from_ip TEXT")
    if "invalidated" not in reset_cols:
        c.execute("ALTER TABLE password_resets ADD COLUMN invalidated INTEGER NOT NULL DEFAULT 0")
    if "token_hash" not in reset_cols:
        c.execute("ALTER TABLE password_resets ADD COLUMN token_hash TEXT")

    # Add jwt_invalidated_before to users. Any session created before this
    # timestamp is considered invalid. Used on password reset + logout-all.
    user_cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    if "jwt_invalidated_before" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN jwt_invalidated_before INTEGER")


def downgrade(c):
    pass  # columns are additive and nullable

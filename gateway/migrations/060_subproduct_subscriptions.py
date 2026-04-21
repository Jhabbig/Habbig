"""Per-subproduct subscription state on ``users``.

Every narve.ai subproduct (sports, weather, world, crypto, midterm,
traders) is a separate Stripe product. A single user can own one, two,
or none; the existing ``subscription_tier`` + per-dashboard subscriptions
model doesn't express "owns exactly the sports subproduct at $19.99/mo"
cleanly, so we add a JSON blob:

    {"sports": {"status": "active", "period_end": 1798419200,
                "stripe_sub_id": "sub_XXX"},
     "crypto": {"status": "past_due", ...}}

JSON (not a relational table) because:

* Read access is always "does user X have subproduct Y?" — a one-row fetch
  followed by a dict lookup, no join cost.
* Writes are low-volume (Stripe webhook per subscription event) and each
  one rewrites the whole blob atomically, which is fine.
* Schema-wise it fits every subproduct we might add without another
  ALTER TABLE.

Pro / enterprise tiers still bypass this — ``has_subproduct_access``
short-circuits to True on those and never reads the blob.

Additive; downgrade clears the column (non-destructive since the default
is ``'{}'``).
"""

from __future__ import annotations

import sqlite3


revision = "060"
down_revision = "059"


def _existing_cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}


def upgrade(c: sqlite3.Connection) -> None:
    user_cols = _existing_cols(c, "users")
    if "subproduct_subscriptions" not in user_cols:
        c.execute(
            "ALTER TABLE users "
            "ADD COLUMN subproduct_subscriptions TEXT NOT NULL DEFAULT '{}'"
        )


def downgrade(c: sqlite3.Connection) -> None:
    # SQLite <3.35 doesn't support DROP COLUMN; matching convention from
    # earlier migrations, leave the column in place on downgrade. Data is
    # a JSON blob with a safe default so nothing depends on its absence.
    pass

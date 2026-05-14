"""Add stripe_customer_id column to users.

Required for the self-service Stripe Customer Portal flow
(/api/billing/portal-session). When Stripe creates a customer for a
narve.ai user via the checkout webhook, we persist the resulting
``cus_…`` ID here so the portal session creator can look it up by
``user_id`` without round-tripping to Stripe.

Additive only — column is nullable and defaults to NULL. The portal
endpoint returns HTTP 400 ("No active subscription") when the column
is empty, which is the correct behaviour for users who never paid.
"""

revision = "185"
down_revision = "184"


def upgrade(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "stripe_customer_id" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    # Lookups by customer_id are used by stripe_webhook_hardening to map
    # an invoice.payment_failed event back to a local user. Index makes
    # that O(log n) instead of a table scan.
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_stripe_customer "
        "ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL"
    )


def downgrade(c):
    c.execute("DROP INDEX IF EXISTS idx_users_stripe_customer")
    try:
        c.execute("ALTER TABLE users DROP COLUMN stripe_customer_id")
    except Exception:  # noqa: BLE001
        pass

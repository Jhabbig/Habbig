"""Mark all invite_tokens as revoked post auth-refactor.

The /token invite-gate was removed on 2026-05-15. invite_tokens rows
are no longer minted. We retain the table for audit history (the
admin email-addresses aggregator still reads target_email from it for
the 'invite' source) but mark every row revoked so they can't be
accidentally honored if the gate ever returns.

Idempotent: skips work if every row is already revoked.
"""
from __future__ import annotations
import time
revision = "198"
down_revision = "197"

def upgrade(c):
    n = c.execute(
        "UPDATE invite_tokens SET status = 'revoked' WHERE status != 'revoked'"
    ).rowcount

def downgrade(c):
    pass  # cannot un-revoke after the gate has been removed

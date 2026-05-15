"""Revoke remaining invite_tokens rows; retain the table for audit history.

Context
-------
The /token invite-gate was removed on 2026-05-15 as part of the auth
refactor (login is now POST /auth/login → create_session). The
``invite_tokens`` table is no longer written by the application —
nothing mints new rows, nothing claims existing ones.

This migration marks every still-unclaimed (or already-claimed) row as
``status = 'revoked'`` so that, if the invite gate is ever re-introduced
in the future, no stale row can be honored to bypass the new flow. It
is idempotent: a second run is a no-op because the UPDATE filter
already excludes ``status = 'revoked'``.

Why we retain (not drop) the table
----------------------------------
The audit pipeline ``aggregate_email_addresses`` in
``gateway/queries/admin.py`` still reads ``invite_tokens.target_email``
for the 'invite' source bucket so admins can audit who was ever invited.
Keeping the historical rows is the point — only the *gate semantics* go
away, not the audit trail.

Dropping the table would also be unsafe right now. ``users`` carries an
``invite_token_id`` FK pointing at ``invite_tokens(id)``. SQLite's only
way to drop a referenced parent is the rename → CREATE → INSERT → DROP
dance, which (per migration 197's bug history) silently rewrites every
other table's stored FK SQL to point at the temporary table name —
leaving dangling references like ``REFERENCES "users_old"(id)`` once
the temp is dropped. We spent today cleaning up exactly that footgun on
``sessions``. Retain + revoke is the conservative choice.

Idempotency
-----------
Short-circuits if there is nothing to revoke: the UPDATE itself is
already a no-op (WHERE status != 'revoked'), but we also peek first so
the migration logs cleanly as "no rows to revoke" on a re-run.
"""

from __future__ import annotations

revision = "198"
down_revision = "197"


def upgrade(c):
    # Short-circuit: nothing to do if every row is already revoked
    # (or the table is empty). Both are idempotent no-ops, but the
    # explicit check keeps the migration log honest on re-runs.
    pending = c.execute(
        "SELECT COUNT(*) AS n FROM invite_tokens "
        "WHERE status != 'revoked'"
    ).fetchone()
    if not pending or pending["n"] == 0:
        return

    c.execute(
        "UPDATE invite_tokens SET status = 'revoked' "
        "WHERE status != 'revoked'"
    )


def downgrade(c):
    # No-op: the invite gate has been removed from the app, so
    # un-revoking these rows would not restore any working flow.
    # Reverting requires re-introducing the gate code path itself.
    pass

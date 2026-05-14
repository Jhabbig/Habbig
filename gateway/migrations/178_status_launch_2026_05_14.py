"""Seed two `info`-severity entries on the public /status timeline for the
2026-05-14 massive landing.

The /status page is the public-facing record of operational events —
outages, deploys, planned maintenance. Today's deploy is a positive
"launching" event: seven new subproducts plus the Permissions-Policy /
COEP / CSP hardening pass. Without these entries the timeline goes
silent on a day that visibly changed the product surface, which makes
the page look stale.

Idempotency is enforced by an INSERT ... WHERE NOT EXISTS guard keyed on
(title, created_at). Re-running the migration is a no-op.

The matching `incident_updates` rows give the timeline a non-empty first
entry — `status_db.create_incident` does this automatically, but we
bypass it here so the migration is plain SQL with no Python-side enum
validation (the canonical `info` + `completed` values are part of this
release; the validator is widened in the same commit).
"""

from __future__ import annotations


revision = "178"
down_revision = "177"


# Both events are bracketed to today's UTC working day. Times are
# epoch seconds — the canonical format for `incidents.created_at` /
# `resolved_at` / `incident_updates.timestamp` (see migration 021).
_DEPLOY_START = 1778738400      # 2026-05-14T06:00:00Z
_DEPLOY_END = 1778796000        # 2026-05-14T22:00:00Z
_SECURITY_START = 1778785200    # 2026-05-14T19:00:00Z (slotted late in the day)
_SECURITY_END = 1778796000      # 2026-05-14T22:00:00Z

_DASHBOARDS_TITLE = "Launching 7 new dashboards"
_DASHBOARDS_BODY = (
    "Today we shipped Voters Atlas, Climate Change, Eco Disasters, "
    "Whale Watch, Central Bank Tracker, World Health, and Love Atlas. "
    "All available at *.narve.ai subdomains."
)

_SECURITY_TITLE = "Security hardening landed"
_SECURITY_BODY = (
    "Expanded Permissions-Policy header (deny camera, mic, USB, etc.), "
    "added Cross-Origin-Resource-Policy, hardened CSP. No user action "
    "required."
)


def _insert_if_missing(c, *, title: str, body: str, created_at: int,
                       resolved_at: int) -> None:
    """Insert an incident + its initial timeline entry exactly once.

    The dedupe key is (title, created_at) — re-running the migration
    against a DB that already has the row is a no-op. We don't use a
    UNIQUE index because the broader `incidents` schema allows duplicate
    titles in general (e.g. repeated "Database slow" outages).
    """
    existing = c.execute(
        "SELECT id FROM incidents WHERE title = ? AND created_at = ?",
        (title, created_at),
    ).fetchone()
    if existing:
        return

    cur = c.execute(
        "INSERT INTO incidents "
        "(created_at, resolved_at, severity, affected_components, "
        " title, description, status, auto_created) "
        "VALUES (?, ?, 'info', '[]', ?, ?, 'completed', 0)",
        (created_at, resolved_at, title, body),
    )
    incident_id = cur.lastrowid

    # Initial timeline entry so the public page shows a non-empty body.
    # Mirrors what status_db.create_incident() would have written.
    c.execute(
        "INSERT INTO incident_updates "
        "(incident_id, timestamp, status, message) "
        "VALUES (?, ?, 'completed', ?)",
        (incident_id, created_at, body),
    )


def upgrade(c) -> None:
    _insert_if_missing(
        c,
        title=_DASHBOARDS_TITLE,
        body=_DASHBOARDS_BODY,
        created_at=_DEPLOY_START,
        resolved_at=_DEPLOY_END,
    )
    _insert_if_missing(
        c,
        title=_SECURITY_TITLE,
        body=_SECURITY_BODY,
        created_at=_SECURITY_START,
        resolved_at=_SECURITY_END,
    )


def downgrade(c) -> None:
    # Remove only the rows this migration created (matched by title +
    # created_at). Cascading FK on incident_updates handles the children.
    for title, ts in (
        (_DASHBOARDS_TITLE, _DEPLOY_START),
        (_SECURITY_TITLE, _SECURITY_START),
    ):
        c.execute(
            "DELETE FROM incidents WHERE title = ? AND created_at = ?",
            (title, ts),
        )

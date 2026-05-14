"""Add segment, frequency, and double-opt-in columns to the newsletter waitlist.

Background:
  The pre-release waitlist (``newsletter_subscribers``) shipped as a single
  bucket — one email, one entry, every subscriber gets every email. As the
  product fans out into subproducts (markets / climate / intelligence /
  election) we need:

    * **segmentation**       — let subscribers pick which subproducts they
                                want to hear about, so we don't send climate
                                disaster digests to someone who signed up
                                for sports markets only.
    * **frequency control**  — weekly digest (default) vs monthly summary
                                vs daily-on-spike, mirroring what authenticated
                                users already get under ``users.email_digest``.
    * **double-opt-in**      — GDPR-clean confirmation flow. A subscriber is
                                not "active" until they click the link in the
                                confirmation email. Until then,
                                ``confirmed_at`` is NULL and no marketing /
                                digest emails are sent.
    * **confirmation token** — single-use, URL-safe, signed via
                                ``GATEWAY_COOKIE_SECRET`` — same construction
                                as ``email_system.unsubscribe.UnsubscribeManager``
                                so we don't introduce a second secret surface.
    * **resend cooldown**    — ``last_confirmation_sent_at`` is the timestamp
                                of the most recent confirmation email. The
                                /api/newsletter handler refuses to re-send if
                                this is less than 24h ago, returning a generic
                                200 so callers can't probe whether an email
                                is already in flight.

Why a migration rather than ALTER in db.py:
  The ad-hoc ALTER blocks in db.py predate the migration runner and remain
  for backwards compatibility, but every new shape change goes through the
  versioned migration system so production deploys get a single, reviewable
  apply. See ``gateway/migrations/__init__.py`` for the runner.

Columns added (all optional; existing rows backfill to safe defaults):

  * ``segment``                   TEXT, NOT NULL DEFAULT 'all'.
        One of: 'all', 'markets', 'election', 'climate', 'intelligence'.
        Validation lives at the API surface (public_routes.py) — SQLite
        can't add CHECK constraints retroactively, and we want to add new
        segments without another migration.

  * ``frequency``                 TEXT, NOT NULL DEFAULT 'weekly'.
        One of: 'weekly', 'monthly', 'daily_spike'.
        Same surface-validation argument as ``segment``.

  * ``confirmation_token``        TEXT, nullable.
        Signed token shipped in the double-opt-in email. NULL once the
        subscriber confirms (we wipe it to invalidate the link).

  * ``confirmed_at``              INTEGER, nullable.
        Unix seconds of confirmation. NULL means "pending double-opt-in".
        Every outbound newsletter MUST filter ``WHERE confirmed_at IS NOT NULL``.

  * ``last_confirmation_sent_at`` INTEGER, nullable.
        Unix seconds of the most recent confirmation email send. The
        resend-cooldown check compares ``now - last_confirmation_sent_at``
        against the 24h window.

  * ``unsubscribed_at``           INTEGER, nullable.
        Unix seconds of unsubscribe. Mirrors ``users.email_unsubscribed_at``
        so the unsubscribe link can flip waitlist rows too.

Backfill semantics:
  Existing rows are pre-launch subscribers from before the segmentation
  shipped — they implicitly opted into "all" segments at weekly frequency.
  The DEFAULTs above handle that. ``confirmed_at`` is back-filled to the
  existing ``subscribed_at`` so legacy subscribers don't get re-prompted
  for confirmation on the first send after deploy (they already trusted
  us with their email and we'd lose them all to confirmation fatigue).
"""

from __future__ import annotations


revision = "177"
down_revision = "176"


def upgrade(cur) -> None:
    existing = {row["name"] for row in cur.execute("PRAGMA table_info(newsletter_subscribers)")}

    if "segment" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN segment TEXT NOT NULL DEFAULT 'all'"
        )
    if "frequency" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN frequency TEXT NOT NULL DEFAULT 'weekly'"
        )
    if "confirmation_token" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN confirmation_token TEXT"
        )
    if "confirmed_at" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN confirmed_at INTEGER"
        )
    if "last_confirmation_sent_at" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN last_confirmation_sent_at INTEGER"
        )
    if "unsubscribed_at" not in existing:
        cur.execute(
            "ALTER TABLE newsletter_subscribers ADD COLUMN unsubscribed_at INTEGER"
        )

    # Backfill: pre-launch rows are implicitly confirmed at their signup time.
    # They already trusted us with their email; forcing a second confirmation
    # would just lose them to confirmation fatigue. New rows from now on
    # require explicit double-opt-in.
    cur.execute(
        "UPDATE newsletter_subscribers SET confirmed_at = subscribed_at "
        "WHERE confirmed_at IS NULL"
    )

    # Index for the confirmation-token lookup endpoint. Token is high-cardinality
    # but only present on unconfirmed rows, so a partial index would be ideal —
    # SQLite supports partial indexes since 3.8.
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_confirmation_token "
        "ON newsletter_subscribers(confirmation_token) "
        "WHERE confirmation_token IS NOT NULL"
    )
    # Index for segment-based digest sends (every weekly job filters on
    # segment + frequency + confirmed_at).
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_segment_frequency "
        "ON newsletter_subscribers(segment, frequency, confirmed_at)"
    )


def downgrade(cur) -> None:
    # SQLite can't DROP COLUMN reliably across versions. The cleanest
    # rollback is recreate-and-copy, which is more invasive than this
    # rollback needs to be. Leave the columns in place — they're nullable
    # or have safe defaults, so older code reads them as no-ops.
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_confirmation_token")
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_segment_frequency")

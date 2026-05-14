"""Newsletter campaign blasts — admin-composed one-offs to confirmed subscribers.

Background:
  The recurring digest (``weekly_digest`` / ``weekly_intelligence``) is
  cron-driven and templated. This table backs the manual side: an admin
  opens ``/admin/newsletter``, composes a markdown body + subject, picks
  a segment + frequency filter, and either sends now or schedules for
  later. Each blast lands here for auditability — who sent what, to how
  many subscribers, on which segment.

Columns:

  * ``id``                   INTEGER PRIMARY KEY.
  * ``admin_user_id``        INTEGER NOT NULL — author. Joined against
                             ``users.id`` at render time; we don't FK so
                             admin row deletes don't take history with them.
  * ``subject``              TEXT NOT NULL — email subject line.
  * ``body_md``              TEXT NOT NULL — raw markdown body. The blast
                             template renders this into HTML at send time.
  * ``segment``              TEXT NOT NULL — 'all' or one of
                             ``VALID_SEGMENTS``. Drives the recipient SQL.
  * ``frequency_filter``     TEXT — nullable; one of ``VALID_FREQUENCIES``
                             or NULL to skip the filter entirely.
  * ``scheduled_at``         INTEGER NOT NULL — unix seconds. Equal to
                             ``created_at`` for "send now" blasts.
  * ``sent_at``              INTEGER — populated after the send completes.
                             NULL on scheduled blasts that haven't fired yet.
  * ``recipient_count``      INTEGER DEFAULT 0 — number of subscribers the
                             send was fanned out to. Useful for admin
                             stats + audit.
  * ``created_at``           INTEGER NOT NULL — row insert time.

Index:
  ``idx_newsletter_campaigns_scheduled`` accelerates the cron-style
  "what's due to send" scan on ``scheduled_at`` for the future
  scheduled-send dispatcher.
"""

from __future__ import annotations


revision = "183"
down_revision = "182"


def upgrade(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS newsletter_campaigns (
          id INTEGER PRIMARY KEY,
          admin_user_id INTEGER NOT NULL,
          subject TEXT NOT NULL,
          body_md TEXT NOT NULL,
          segment TEXT NOT NULL,
          frequency_filter TEXT,
          scheduled_at INTEGER NOT NULL,
          sent_at INTEGER,
          recipient_count INTEGER DEFAULT 0,
          created_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_newsletter_campaigns_scheduled "
        "ON newsletter_campaigns(scheduled_at)"
    )


def downgrade(cur) -> None:
    cur.execute("DROP INDEX IF EXISTS idx_newsletter_campaigns_scheduled")
    cur.execute("DROP TABLE IF EXISTS newsletter_campaigns")

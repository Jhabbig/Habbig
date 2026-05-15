"""Quiet-hours gate for user-facing notification jobs.

Audit finding (HIGHx4): four hourly user-impacting jobs fire push and
email notifications at 03:00 UK time — well inside the window the user
is asleep. This module exposes a single helper, ``_within_quiet_hours``,
that the notification-emit step of each affected job consults to early-
exit during the quiet window. The data-pass step still runs every tick;
only the fan-out is suppressed, and the next non-quiet tick picks up
whatever was queued.

Window
------
UTC hour in ``[22, 6)``. That covers UK winter (GMT, UTC+0) 22:00-06:00
and UK summer (BST, UTC+1) 23:00-07:00 in the same single window — no
DST branching required because we shift in UTC, not local time.

The helper takes an optional ``now`` for deterministic testing.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional


QUIET_START_UTC_HOUR = 22  # inclusive
QUIET_END_UTC_HOUR = 6     # exclusive


def _within_quiet_hours(now: Optional[_dt.datetime] = None) -> bool:
    """Return True if the current UTC hour is inside the quiet window.

    The window is ``[22, 6)`` UTC, which maps to UK winter 22:00-06:00
    and UK summer 23:00-07:00. Notification-emit steps wrap their work
    in ``if _within_quiet_hours(): return`` and rely on the next non-
    quiet cron tick to drain whatever the data-pass step queued.
    """
    current = now if now is not None else _dt.datetime.utcnow()
    hour = current.hour
    return hour >= QUIET_START_UTC_HOUR or hour < QUIET_END_UTC_HOUR

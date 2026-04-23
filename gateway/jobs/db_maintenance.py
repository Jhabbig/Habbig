"""Scheduled DB maintenance jobs.

Three things the SQLite file needs on a recurring cadence that nothing
else in the codebase runs:

  1. ``wal_checkpoint(TRUNCATE)`` — the WAL file grows unbounded under
     write load; occasional truncation stops it eating disk and keeps
     readers from walking an oversized journal. We run this nightly
     at 04:10 UTC (after the credibility recompute at 04:00 and
     before the source-summary regen at 04:30, both of which hit
     concurrent reads).

  2. ``VACUUM`` — rebuilds the DB file and reclaims pages freed by
     deletes. Cheap when the DB is small; potentially blocking once
     the file grows, so we run it quarterly (first Sunday of Jan / Apr
     / Jul / Oct at 05:00 UTC). The ``apscheduler`` cron surface
     doesn't support "first Sunday of the month" directly, so we gate
     on ``day=1..7`` + ``weekday=6`` inside the handler.

  3. Retention trims — slow_request_log and slow_query_log both grow
     indefinitely. Keep 30 days.

Every job is fire-and-forget: a failure logs and swallows rather than
taking the scheduler down. None of them hit user requests.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any

from jobs.registry import register_cron, register_job


log = logging.getLogger("jobs.db_maintenance")


# ── WAL checkpoint ────────────────────────────────────────────────────────


@register_job("wal_checkpoint")
async def wal_checkpoint() -> dict[str, Any]:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` on the main DB.

    Returns the three-int result tuple from SQLite:
      (busy, log_pages, checkpointed_pages)
    so the admin panel can see if a checkpoint keeps failing because
    of a long-lived reader (``busy>0``).
    """
    import db
    try:
        with db.conn() as c:
            row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # sqlite3.Row doesn't round-trip to JSON. Extract by index.
        busy, log_pages, checkpointed = (row[0], row[1], row[2]) if row else (None, None, None)
        log.info(
            "wal_checkpoint(TRUNCATE): busy=%s log_pages=%s checkpointed=%s",
            busy, log_pages, checkpointed,
        )
        return {
            "ok": True,
            "busy": busy,
            "log_pages": log_pages,
            "checkpointed": checkpointed,
        }
    except Exception as e:
        log.exception("wal_checkpoint failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── VACUUM ────────────────────────────────────────────────────────────────


@register_job("vacuum_db_maybe")
async def vacuum_db_maybe() -> dict[str, Any]:
    """VACUUM the DB on the first Sunday of the quarter.

    The cron slot fires every Sunday at 05:00. We gate on month + day
    inside the handler because apscheduler cron can't express "first
    Sunday of Jan / Apr / Jul / Oct" natively.
    """
    now = _dt.datetime.utcnow()
    if now.month not in (1, 4, 7, 10):
        return {"ok": True, "skipped": "not quarter month"}
    if now.day > 7:
        return {"ok": True, "skipped": "past first week"}

    import db
    t0 = time.time()
    try:
        with db.conn() as c:
            c.execute("VACUUM")
        duration = int((time.time() - t0) * 1000)
        log.info("VACUUM completed in %d ms", duration)
        return {"ok": True, "duration_ms": duration}
    except Exception as e:
        log.exception("VACUUM failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── Retention trims ───────────────────────────────────────────────────────


@register_job("trim_perf_logs")
async def trim_perf_logs(days: int = 30) -> dict[str, Any]:
    """Keep ``slow_request_log`` and ``slow_query_log`` bounded.

    30 days gives the admin performance page enough week-over-week
    history to spot drift without letting the two tables bloat the DB.
    """
    import db
    cutoff = int(time.time()) - int(days) * 86400
    removed = {"slow_request_log": 0, "slow_query_log": 0}
    for table in removed:
        try:
            with db.conn() as c:
                cur = c.execute(
                    f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,)
                )
                removed[table] = cur.rowcount or 0
        except Exception as e:
            # Table may not exist on a stale DB that hasn't run
            # migrations 081/096 yet — swallow so a single missing
            # table doesn't block the other trim.
            log.warning("trim_perf_logs: %s skipped — %s", table, e)
    return {"ok": True, "removed": removed, "cutoff_ts": cutoff}


# ── Schedule ──────────────────────────────────────────────────────────────

# 04:10 UTC daily — well clear of the 04:00 credibility recompute and
# the 04:30 source-summary regen, both of which hold read connections.
register_cron("wal_checkpoint", hour=4, minute=10)

# Every Sunday at 05:00 UTC; the job's own gate short-circuits everything
# except the first Sunday of Jan/Apr/Jul/Oct. Cheap to call 49 extra
# times per year.
register_cron("vacuum_db_maybe", hour=5, minute=0, weekday=6)

# 03:40 UTC daily — runs before the WAL checkpoint so truncation
# reflects the trimmed state.
register_cron("trim_perf_logs", hour=3, minute=40)


# ── Quarterly recovery drill ──────────────────────────────────────────────


@register_job("recovery_drill")
async def recovery_drill() -> dict[str, Any]:
    """Prove the restore path works before the day it matters.

    1. Snapshot the live DB via SQLite's .backup API into a tmp file.
    2. Open the copy read-only; run integrity_check + foreign_key_check.
    3. Compare row counts on two core tables between live and restore.
       The online .backup is atomic per-row so counts should match
       exactly modulo any writes that happened mid-backup — we allow
       a 1% slop before flagging.
    4. Append one row to drill_runs so /admin/backups has history.
    5. Delete the tmp file.

    Runs every 90 days via a day-of-month gate (first of Jan/Apr/Jul/Oct).
    Every other day short-circuits as a near-no-op.
    """
    import os
    import sqlite3
    import tempfile

    import db

    now = int(time.time())
    today = _dt.datetime.utcnow()
    if not (today.day == 1 and today.month in (1, 4, 7, 10)):
        return {"skipped": "not a quarterly drill day"}

    tmp = tempfile.NamedTemporaryFile(prefix="drill_", suffix=".db", delete=False)
    tmp.close()
    drill_path = tmp.name
    integrity_ok = False
    fk_ok = False
    users_live = users_restore = None
    preds_live = preds_restore = None
    note_parts: list[str] = []

    try:
        with db.conn() as live_conn:
            dest = sqlite3.connect(drill_path)
            try:
                # sqlite3.Connection.backup requires a real connection;
                # the pooled db.conn() yield wraps one, and the backup
                # API holds the source DB's writer lock only during
                # each batch — safe alongside other writers.
                live_conn.backup(dest)
            finally:
                dest.close()
            users_live = live_conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
            try:
                preds_live = live_conn.execute(
                    "SELECT COUNT(*) FROM predictions"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                preds_live = 0

        # Restore-side: open read-only so a bug here can't mutate the
        # drill copy into looking healthy.
        ro_uri = f"file:{drill_path}?mode=ro"
        restore_conn = sqlite3.connect(ro_uri, uri=True)
        try:
            integ = restore_conn.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(integ and integ[0] == "ok")
            fk_rows = restore_conn.execute("PRAGMA foreign_key_check").fetchall()
            fk_ok = len(fk_rows) == 0
            users_restore = restore_conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]
            try:
                preds_restore = restore_conn.execute(
                    "SELECT COUNT(*) FROM predictions"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                preds_restore = 0
        finally:
            restore_conn.close()

        def _divergent(live: int, restore: int) -> bool:
            if not live:
                return bool(restore)
            return abs(live - restore) / max(live, 1) > 0.01

        if _divergent(users_live or 0, users_restore or 0):
            note_parts.append(
                f"users divergence live={users_live} restore={users_restore}"
            )
        if _divergent(preds_live or 0, preds_restore or 0):
            note_parts.append(
                f"predictions divergence live={preds_live} restore={preds_restore}"
            )

    except Exception as exc:  # pragma: no cover - defensive
        log.exception("recovery_drill failed mid-run")
        note_parts.append(f"error: {exc}")
    finally:
        try:
            os.unlink(drill_path)
        except OSError:
            pass

    notes = " | ".join(note_parts) or "ok"
    try:
        with db.conn() as c:
            c.execute(
                "INSERT INTO drill_runs "
                "(started_at, completed_at, integrity_ok, foreign_key_ok, "
                " users_live, users_restore, predictions_live, predictions_restore, "
                " backup_source, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, int(time.time()),
                    int(integrity_ok), int(fk_ok),
                    users_live, users_restore,
                    preds_live, preds_restore,
                    "live+sqlite_backup_api",
                    notes[:2000],
                ),
            )
    except Exception:
        log.exception("recovery_drill: drill_runs insert failed")

    return {
        "integrity_ok": integrity_ok,
        "foreign_key_ok": fk_ok,
        "users_live": users_live, "users_restore": users_restore,
        "predictions_live": preds_live, "predictions_restore": preds_restore,
        "notes": notes,
    }


# 05:20 UTC daily — the job gates on first-of-Jan/Apr/Jul/Oct. Slotted
# after vacuum_db_maybe (05:00) so a quarterly VACUUM finishes first.
register_cron("recovery_drill", hour=5, minute=20)

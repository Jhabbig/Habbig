"""Scheduled DB maintenance jobs.

Four things the SQLite file needs on a recurring cadence that nothing
else in the codebase runs:

  1. ``wal_checkpoint(TRUNCATE)`` — the WAL file grows unbounded under
     write load; occasional truncation stops it eating disk and keeps
     readers from walking an oversized journal. We run this nightly
     at 04:10 UTC (after the credibility recompute at 04:00 and
     before the source-summary regen at 04:30, both of which hit
     concurrent reads). The daily VACUUM job (#2) also calls this at
     05:00 — keeping the 04:10 standalone slot gives us a checkpoint
     during the active window in case the 05:00 VACUUM fails.

  2. Daily ``VACUUM`` + ``ANALYZE`` + ``wal_checkpoint(TRUNCATE)`` on
     ``auth.db``. Per the perf audit (PERFORMANCE_BASELINE.md), the
     DB grows enough between deploys that monthly VACUUM is too coarse
     — daily keeps the file compact and re-runs ``ANALYZE`` so the
     query planner has fresh statistics. Runs at 05:00 UTC, well clear
     of the credibility recompute (04:00) and source-summary regen
     (04:30). VACUUM rebuilds the file (effectively truncating the
     WAL) but we still issue the explicit ``wal_checkpoint(TRUNCATE)``
     afterwards as a belt-and-braces safety net.

  3. Retention trims — slow_request_log and slow_query_log both grow
     indefinitely. Keep 30 days.

  4. Quarterly recovery drill — proves the .backup restore path works
     by integrity-checking a copy of the live DB.

Every job is fire-and-forget: a failure logs and swallows rather than
taking the scheduler down. None of them hit user requests.

Subproduct DBs (voters.sqlite, whale.sqlite, annoyance.db, love.sqlite)
are NOT touched from here — they live in independent service processes
with their own startup VACUUM hooks (see each subproduct's server.py).
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


# ── VACUUM (daily) ────────────────────────────────────────────────────────


def _db_file_size_bytes() -> int | None:
    """Return on-disk size of the main DB file, or ``None`` if unknown.

    Helper isolated so tests can monkey-patch it. We use ``os.path.getsize``
    against ``db.DB_PATH`` because that gives the actual filesystem footprint
    that VACUUM reclaims — not the logical page count, which would miss
    fragmentation savings.
    """
    try:
        import os
        import db
        return os.path.getsize(db.DB_PATH)
    except Exception:
        return None


@register_job("vacuum_db_daily")
async def vacuum_db_daily() -> dict[str, Any]:
    """Daily VACUUM + ANALYZE + WAL-truncate on ``auth.db``.

    Sequence:
      1. Record on-disk size before.
      2. ``VACUUM`` — rebuilds the DB file, reclaims pages freed by
         deletes, defragments tables. Holds an exclusive lock so we
         schedule for the quiet 05:00 UTC slot.
      3. ``ANALYZE`` — refreshes ``sqlite_stat1`` so the query planner
         picks up new selectivity after data churn.
      4. ``PRAGMA wal_checkpoint(TRUNCATE)`` — VACUUM already rolls the
         WAL into the main file, but the explicit truncate is harmless
         and guarantees no orphan WAL pages survive.
      5. Record on-disk size after, log delta.

    Lock contention: if another writer holds the DB, sqlite3 raises
    ``sqlite3.OperationalError: database is locked``. We swallow and
    log so a transient contender doesn't take the scheduler down — the
    next nightly run will retry.
    """
    import sqlite3
    import db

    size_before = _db_file_size_bytes()
    t0 = time.time()
    vacuum_ok = analyze_ok = wal_ok = False
    wal_result: tuple[Any, Any, Any] | None = None
    error: str | None = None

    try:
        with db.conn() as c:
            c.execute("VACUUM")
            vacuum_ok = True
            c.execute("ANALYZE")
            analyze_ok = True
            row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row is not None:
                wal_result = (row[0], row[1], row[2])
            wal_ok = True
    except sqlite3.OperationalError as e:
        # Most common: database is locked. Log and bail — the job will
        # rerun tomorrow. Don't propagate so scheduler stays healthy.
        error = f"OperationalError: {e}"
        log.warning("vacuum_db_daily: lock contention or operational error — %s", e)
    except Exception as e:
        error = str(e)
        log.exception("vacuum_db_daily failed: %s", e)

    size_after = _db_file_size_bytes()
    duration_ms = int((time.time() - t0) * 1000)

    if size_before is not None and size_after is not None:
        delta = size_before - size_after
        log.info(
            "vacuum_db_daily: size_before=%dB size_after=%dB delta=%dB duration=%dms"
            " vacuum_ok=%s analyze_ok=%s wal_ok=%s",
            size_before, size_after, delta, duration_ms,
            vacuum_ok, analyze_ok, wal_ok,
        )
    else:
        log.info(
            "vacuum_db_daily: size unavailable duration=%dms"
            " vacuum_ok=%s analyze_ok=%s wal_ok=%s",
            duration_ms, vacuum_ok, analyze_ok, wal_ok,
        )

    result: dict[str, Any] = {
        "ok": vacuum_ok and analyze_ok and wal_ok,
        "vacuum_ok": vacuum_ok,
        "analyze_ok": analyze_ok,
        "wal_ok": wal_ok,
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "duration_ms": duration_ms,
    }
    if wal_result is not None:
        result["wal_busy"], result["wal_log_pages"], result["wal_checkpointed"] = wal_result
    if error is not None:
        result["error"] = error
    return result


# Back-compat alias: the previous quarterly job was named
# ``vacuum_db_maybe``. Anything that enqueues it by name (admin tools,
# old retry rows in background_jobs) should still run — point them at
# the new daily handler. Apscheduler cron registrations are wired to
# the new name below, so this alias is queue-driven only.
@register_job("vacuum_db_maybe")
async def vacuum_db_maybe() -> dict[str, Any]:
    """Deprecated alias — calls :func:`vacuum_db_daily`."""
    return await vacuum_db_daily()


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


@register_job("trim_job_runs")
async def trim_job_runs(days: int = 30) -> dict[str, Any]:
    """Trim ``job_runs`` rows older than ``days``.

    Per the perf audit, the admin ``/admin/jobs`` page polls every 5s
    and runs three full-table scans against ``job_runs`` per render
    (last-run, avg-duration, recent-failures). With nothing bounding
    the table, every scheduled job tick (hundreds per day across the
    full registry) leaves a row that never goes away — the table will
    eventually dominate scan cost. 30 days is plenty for week-over-week
    failure-rate trends while keeping the table small enough that the
    admin polling loop stays cheap.

    The migration column is ``completed_at`` (not ``finished_at``); we
    delete on that so still-running rows (NULL completed_at) are never
    swept while they're active.
    """
    import db
    cutoff = int(time.time()) - int(days) * 86400
    try:
        with db.conn() as c:
            cur = c.execute(
                "DELETE FROM job_runs WHERE completed_at IS NOT NULL "
                "AND completed_at < ?",
                (cutoff,),
            )
            removed = cur.rowcount or 0
        log.info("trim_job_runs: removed %d rows < %d", removed, cutoff)
        return {"ok": True, "removed": removed, "cutoff_ts": cutoff}
    except Exception as e:
        log.warning("trim_job_runs: skipped — %s", e)
        return {"ok": False, "error": str(e), "cutoff_ts": cutoff}


@register_job("trim_wallet_connect_nonces")
async def trim_wallet_connect_nonces(max_age_seconds: int = 3600) -> dict[str, Any]:
    """Sweep SIWE wallet-connect nonces older than ``max_age_seconds``.

    Live nonces have a 5-minute TTL (see SIWE_NONCE_TTL in
    market_routes); 1 hour is a comfortable retention window — well
    past any in-flight wallet UX and useful as forensic context if
    we're investigating a connect-replay attempt the same day. After
    that the row is just exhaust. Both used and unused rows are swept;
    a never-redeemed nonce older than 1h is dead either way.

    Idempotent and fire-and-forget — a missing table (clean install
    that hasn't run migration 179 yet) logs and returns ok=False
    instead of taking the scheduler down.
    """
    import db
    cutoff = int(time.time()) - int(max_age_seconds)
    try:
        with db.conn() as c:
            cur = c.execute(
                "DELETE FROM wallet_connect_nonces WHERE created_at < ?",
                (cutoff,),
            )
            removed = cur.rowcount or 0
        log.info("trim_wallet_connect_nonces: removed %d rows < %d",
                 removed, cutoff)
        return {"ok": True, "removed": removed, "cutoff_ts": cutoff}
    except Exception as e:
        log.warning("trim_wallet_connect_nonces: skipped — %s", e)
        return {"ok": False, "error": str(e), "cutoff_ts": cutoff}


# ── Schedule ──────────────────────────────────────────────────────────────

# 04:10 UTC daily — well clear of the 04:00 credibility recompute and
# the 04:30 source-summary regen, both of which hold read connections.
# Redundant with the WAL truncate inside vacuum_db_daily (05:00) but
# cheap, and gives us coverage if the 05:00 job fails.
register_cron("wal_checkpoint", hour=4, minute=10)

# 05:00 UTC daily — daily VACUUM + ANALYZE + WAL-truncate per the perf
# audit. Slotted after credibility recompute (04:00), source-summary
# regen (04:30), and the standalone WAL checkpoint (04:10) — none of
# them hold an exclusive lock, but stacking gives VACUUM a clean window.
register_cron("vacuum_db_daily", hour=5, minute=0)

# 03:40 UTC daily — runs before the WAL checkpoint so truncation
# reflects the trimmed state.
register_cron("trim_perf_logs", hour=3, minute=40)

# 03:45 UTC daily — SIWE wallet-connect nonces older than 1h are
# swept. Slotted between the perf-log trim and the WAL checkpoint so
# the deletions land in the same nightly checkpoint cycle.
register_cron("trim_wallet_connect_nonces", hour=3, minute=45)

# 04:15 UTC daily — trims scheduled-job history. Slotted between the
# 04:10 WAL checkpoint and the 04:30 source-summary regen so the
# deletes land before the daily VACUUM at 05:00 reclaims the pages.
# 30-day retention bounds /admin/jobs polling scans per the perf audit.
register_cron("trim_job_runs", hour=4, minute=15)


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
# after vacuum_db_daily (05:00) so the daily VACUUM finishes first and
# the .backup runs against a freshly compacted file.
register_cron("recovery_drill", hour=5, minute=20)

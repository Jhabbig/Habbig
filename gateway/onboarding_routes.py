"""First-run experience — guided tour, sample data, first-week goals, admin metrics.

Scope is strictly post-token-gate: every route here assumes the user is
already authenticated (the token/login flows are owned elsewhere).

Surfaces:

  GET  /onboarding                      5-step guided tour shell (static HTML)
  POST /api/onboarding/advance          update step_completed
  POST /api/onboarding/dismiss          skip tour → dashboard
  POST /api/onboarding/categories       save topic picks (step 2)
  POST /api/onboarding/follow-sources   create follow rows + mark goal (step 3)
  POST /api/onboarding/notifications    mark push enabled (step 4)
  POST /api/onboarding/complete         stamp completed_at + redirect
  GET  /api/onboarding/suggested-sources?categories=...
  GET  /api/onboarding/sample-signal    highest-EV active signal (step 5)

  GET  /api/first-week/goals            checklist state for the widget
  POST /api/first-week/goals/{key}      mark a goal complete (idempotent)
  POST /api/first-week/widget/dismiss   hide the widget forever

  GET  /api/feed/sample                 5 starter predictions for empty dashboard

  GET  /admin/onboarding                HTML metrics page (admin-only)
  GET  /admin/api/onboarding/metrics    JSON rollup (admin-only)

Wire via ``onboarding_routes.register(app)``. Module keeps its own DB
connection so it never tangles with server.py or db.py hot paths.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html as _html
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


log = logging.getLogger("onboarding_routes")


FIRST_WEEK_DAYS = 14  # widget auto-hides after 14 days even if goals open
ALL_GOALS = (
    "follow_3_sources",
    "save_1_prediction",
    "enable_notifications",
    "visit_5_distinct_tabs",
    "view_1_market_detail",
    "complete_first_prediction",
)
GOAL_LABELS: dict[str, str] = {
    "follow_3_sources":           "Follow 3 sources",
    "save_1_prediction":          "Save 1 prediction",
    "enable_notifications":       "Enable notifications",
    "visit_5_distinct_tabs":      "Visit 5 different tabs",
    "view_1_market_detail":       "View 1 market",
    "complete_first_prediction":  "Make 1 prediction",
}

VALID_CATEGORIES = {
    "politics", "sports", "crypto", "geopolitics", "finance", "weather",
}


# ── DB helpers ──────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def _require_user(request: Request) -> dict:
    """Fetch the authenticated user from server.current_user — lazy import
    so this module doesn't force server.py to load when only routes are
    being registered.
    """
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    return user


def _require_admin(request: Request) -> dict:
    import server
    user = server._require_admin_user(request)
    if user is None:
        raise HTTPException(status_code=403, detail="Admin required")
    return user


# ── Onboarding state read/write ─────────────────────────────────────────────


def _get_onboarding_row(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    if not _table_exists(conn, "user_onboarding"):
        return None
    row = conn.execute(
        "SELECT * FROM user_onboarding WHERE user_id = ?", (user_id,),
    ).fetchone()
    return dict(row) if row else None


def _ensure_onboarding_row(conn: sqlite3.Connection, user_id: int) -> dict:
    existing = _get_onboarding_row(conn, user_id)
    if existing:
        return existing
    now = int(time.time())
    conn.execute(
        "INSERT INTO user_onboarding (user_id, started_at, step_completed) "
        "VALUES (?, ?, 0)",
        (user_id, now),
    )
    conn.commit()
    return {
        "user_id": user_id, "started_at": now, "completed_at": None,
        "step_completed": 0, "dismissed": 0, "goals_completed": "{}",
        "widget_dismissed_at": None,
    }


def _set_goal(conn: sqlite3.Connection, user_id: int, goal_key: str) -> None:
    if goal_key not in ALL_GOALS:
        return
    if not _table_exists(conn, "user_first_week_goals"):
        return
    conn.execute(
        "INSERT OR IGNORE INTO user_first_week_goals "
        "(user_id, goal_key, completed_at) VALUES (?, ?, ?)",
        (user_id, goal_key, int(time.time())),
    )
    conn.execute(
        "UPDATE user_first_week_goals SET completed_at = COALESCE(completed_at, ?) "
        "WHERE user_id = ? AND goal_key = ?",
        (int(time.time()), user_id, goal_key),
    )
    conn.commit()


# ── /onboarding shell ───────────────────────────────────────────────────────


async def onboarding_page(request: Request):
    user = _require_user(request)
    import server
    # Static template already lives at static/onboarding.html — reuse it.
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
    finally:
        conn.close()
    username = user.get("username") or user.get("email", "").split("@")[0]
    first_name = username.split(".")[0].split("_")[0].title()
    return server.render_page(
        "onboarding",
        request=request,
        email=user.get("email", ""),
        username=username,
        first_name=first_name or username,
    )


async def advance_step(request: Request, step: int = Form(...)):
    user = _require_user(request)
    step = max(0, min(5, int(step)))
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        conn.execute(
            "UPDATE user_onboarding SET step_completed = MAX(step_completed, ?) "
            "WHERE user_id = ?",
            (step, user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"step": step, "ok": True})


async def dismiss_tour(request: Request):
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        now = int(time.time())
        conn.execute(
            "UPDATE user_onboarding SET dismissed = 1, completed_at = COALESCE(completed_at, ?) "
            "WHERE user_id = ?",
            (now, user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"dismissed": True, "redirect": "/dashboard"})


async def save_categories(request: Request, categories: str = Form("")):
    user = _require_user(request)
    picked = [c.strip().lower() for c in (categories or "").split(",") if c.strip()]
    picked = [c for c in picked if c in VALID_CATEGORIES][:3]
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        # Store both in user_onboarding.goals_completed (light cache) and
        # the canonical users.onboarding_categories column if present.
        row = _get_onboarding_row(conn, user["user_id"]) or {}
        goals = {}
        try:
            goals = json.loads(row.get("goals_completed") or "{}")
        except json.JSONDecodeError:
            goals = {}
        goals["categories"] = picked
        conn.execute(
            "UPDATE user_onboarding SET goals_completed = ?, step_completed = MAX(step_completed, 2) "
            "WHERE user_id = ?",
            (json.dumps(goals), user["user_id"]),
        )

        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "onboarding_categories" in user_cols:
            conn.execute(
                "UPDATE users SET onboarding_categories = ? WHERE id = ?",
                (json.dumps(picked), user["user_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"saved": True, "categories": picked})


# ── Suggested sources ──────────────────────────────────────────────────────


async def suggested_sources(request: Request):
    """Top-3 most-credible sources per picked category.

    No Claude calls, pure SQL against source_credibility +
    source_category_credibility (when present). Returns at most 9.
    """
    _require_user(request)
    categories = [c.strip().lower() for c in
                  (request.query_params.get("categories") or "").split(",")
                  if c.strip() and c.strip().lower() in VALID_CATEGORIES][:3]
    if not categories:
        categories = ["politics", "finance", "crypto"]

    conn = _connect()
    try:
        if not _table_exists(conn, "source_credibility"):
            return JSONResponse({"sources": [], "categories": categories})

        has_cat_table = _table_exists(conn, "source_category_credibility")
        suggestions: list[dict] = []
        seen: set[str] = set()

        for cat in categories:
            if has_cat_table:
                rows = conn.execute(
                    """
                    SELECT sc.source_handle, sc.global_credibility,
                           sc.total_predictions, scc.category
                    FROM source_category_credibility scc
                    JOIN source_credibility sc
                      ON sc.source_handle = scc.source_handle
                    WHERE scc.category = ? AND sc.accuracy_unlocked = 1
                    ORDER BY scc.category_credibility DESC, sc.global_credibility DESC
                    LIMIT 3
                    """,
                    (cat,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source_handle, global_credibility, total_predictions, "
                    "  NULL AS category "
                    "FROM source_credibility WHERE accuracy_unlocked = 1 "
                    "ORDER BY global_credibility DESC LIMIT 3"
                ).fetchall()

            for r in rows:
                h = r["source_handle"]
                if h in seen:
                    continue
                seen.add(h)
                suggestions.append({
                    "handle": h,
                    "category": r["category"] or cat,
                    "credibility": round(float(r["global_credibility"] or 0.5), 3),
                    "total_predictions": int(r["total_predictions"] or 0),
                })
    finally:
        conn.close()
    return JSONResponse({"sources": suggestions, "categories": categories})


async def follow_sources(request: Request, handles: str = Form("")):
    """Create followed-sources rows for each handle the user ticked.

    Tolerant of two common schemas:
      - followed_sources(user_id, source_handle)           (newer)
      - user_source_follows(user_id, source_handle)        (older)
    Missing both → record the list only in goals_completed JSON so the
    tour still progresses.
    """
    user = _require_user(request)
    picked = [h.strip().lstrip("@") for h in (handles or "").split(",") if h.strip()]
    picked = picked[:10]
    if not picked:
        return JSONResponse({"followed": [], "goal_triggered": False})

    conn = _connect()
    followed = 0
    try:
        target_table = None
        for name in ("followed_sources", "user_source_follows", "user_follows"):
            if _table_exists(conn, name):
                target_table = name
                break
        now = int(time.time())
        if target_table:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({target_table})")}
            has_created = "created_at" in cols
            for h in picked:
                try:
                    if has_created:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {target_table} "
                            f"(user_id, source_handle, created_at) VALUES (?, ?, ?)",
                            (user["user_id"], h, now),
                        )
                    else:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {target_table} "
                            f"(user_id, source_handle) VALUES (?, ?)",
                            (user["user_id"], h),
                        )
                    followed += 1
                except sqlite3.Error:
                    continue

        # Update onboarding JSON as a belt-and-braces record.
        row = _get_onboarding_row(conn, user["user_id"]) or {}
        try:
            goals = json.loads(row.get("goals_completed") or "{}")
        except json.JSONDecodeError:
            goals = {}
        goals["follow_source"] = True
        goals["followed_handles"] = picked
        _ensure_onboarding_row(conn, user["user_id"])
        conn.execute(
            "UPDATE user_onboarding SET goals_completed = ?, step_completed = MAX(step_completed, 3) "
            "WHERE user_id = ?",
            (json.dumps(goals), user["user_id"]),
        )
        conn.commit()

        if len(picked) >= 3:
            _set_goal(conn, user["user_id"], "follow_3_sources")
    finally:
        conn.close()
    return JSONResponse({
        "followed": picked, "persisted": followed,
        "goal_triggered": len(picked) >= 3,
    })


async def notifications_enabled(request: Request, enabled: int = Form(1)):
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        row = _get_onboarding_row(conn, user["user_id"]) or {}
        try:
            goals = json.loads(row.get("goals_completed") or "{}")
        except json.JSONDecodeError:
            goals = {}
        goals["enable_notifications"] = bool(enabled)
        conn.execute(
            "UPDATE user_onboarding SET goals_completed = ?, step_completed = MAX(step_completed, 4) "
            "WHERE user_id = ?",
            (json.dumps(goals), user["user_id"]),
        )
        # Mirror into users.notify_push if present.
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "notify_push" in user_cols:
            conn.execute(
                "UPDATE users SET notify_push = ? WHERE id = ?",
                (1 if enabled else 0, user["user_id"]),
            )
        conn.commit()
        if enabled:
            _set_goal(conn, user["user_id"], "enable_notifications")
    finally:
        conn.close()
    return JSONResponse({"enabled": bool(enabled), "goal_triggered": bool(enabled)})


async def complete_onboarding(request: Request):
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        now = int(time.time())
        conn.execute(
            "UPDATE user_onboarding SET completed_at = ?, step_completed = 5 "
            "WHERE user_id = ?",
            (now, user["user_id"]),
        )
        user_cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "onboarding_completed" in user_cols:
            conn.execute(
                "UPDATE users SET onboarding_completed = 1, onboarding_completed_at = ? "
                "WHERE id = ?",
                (now, user["user_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"completed": True, "redirect": "/dashboard?first_visit=1"})


# ── Sample signal (step 5) ─────────────────────────────────────────────────


async def sample_signal(request: Request):
    _require_user(request)
    conn = _connect()
    try:
        row = None
        if _table_exists(conn, "best_bets"):
            row = conn.execute(
                "SELECT * FROM best_bets "
                "WHERE resolved_at IS NULL OR resolved_at = 0 "
                "ORDER BY edge_pct DESC, id DESC LIMIT 1"
            ).fetchone()
        if not row and _table_exists(conn, "predictions"):
            row = conn.execute(
                "SELECT * FROM predictions "
                "WHERE resolved = 0 ORDER BY extracted_at DESC LIMIT 1"
            ).fetchone()

        if not row:
            return JSONResponse({
                "signal": None,
                "narrative": (
                    "narve.ai is still warming up for you — your first "
                    "signal arrives once the pipeline has fresh predictions."
                ),
            })

        signal = dict(row)
        narrative = _sample_narrative(signal)
        return JSONResponse({"signal": signal, "narrative": narrative})
    finally:
        conn.close()


def _sample_narrative(signal: dict) -> str:
    market = signal.get("content") or signal.get("market_title") or "this market"
    edge = signal.get("edge_pct") or signal.get("predicted_probability")
    source = signal.get("source_handle") or "an early source"
    edge_str = f"{edge:.0%}" if isinstance(edge, float) and 0 <= edge <= 1 else "notable edge"
    return (
        f"Right now, narve.ai's strongest signal comes from @{source}: "
        f"\"{market[:180]}\". The credibility-weighted edge is {edge_str}. "
        f"Save it to your watchlist and you'll see the resolution land."
    )


# ── Sample feed for empty dashboards ────────────────────────────────────────


STARTER_PREDICTIONS = [
    {"source_handle": "fedwatcher",   "content": "Fed holds rates at next FOMC meeting",
     "category": "finance",    "direction": "YES", "edge": 0.12},
    {"source_handle": "pollster",     "content": "Incumbent wins re-election in competitive race",
     "category": "politics",   "direction": "YES", "edge": 0.08},
    {"source_handle": "cryptoquant",  "content": "BTC closes above $70k by quarter end",
     "category": "crypto",     "direction": "YES", "edge": 0.15},
    {"source_handle": "geopolsig",    "content": "Conflict reaches 12-month ceasefire milestone",
     "category": "geopolitics", "direction": "NO",  "edge": 0.09},
    {"source_handle": "sharpedesk",   "content": "Home favourite covers in Sunday primetime NFL",
     "category": "sports",     "direction": "YES", "edge": 0.06},
]


async def sample_feed(request: Request):
    """Render-time sample data. Always returns the same 5 items —
    frontend badges them as "sample" and auto-hides once the user has
    10+ real items (client decides).
    """
    _require_user(request)
    return JSONResponse({
        "sample": True,
        "note": "This is a sample view. Your feed fills in as you follow sources.",
        "predictions": STARTER_PREDICTIONS,
    })


# ── First-week goals ────────────────────────────────────────────────────────


async def goals_state(request: Request):
    user = _require_user(request)
    conn = _connect()
    try:
        if not _table_exists(conn, "user_first_week_goals"):
            return JSONResponse({"goals": [], "dismissed": False, "days_since_signup": 0})
        rows = conn.execute(
            "SELECT goal_key, completed_at FROM user_first_week_goals WHERE user_id = ?",
            (user["user_id"],),
        ).fetchall()
        by_key = {r["goal_key"]: r["completed_at"] for r in rows}

        onboard = _get_onboarding_row(conn, user["user_id"]) or {}
        widget_dismissed_at = onboard.get("widget_dismissed_at")

        # Days since signup — use users.created_at when present.
        started_at = onboard.get("started_at")
        if not started_at:
            try:
                row = conn.execute(
                    "SELECT created_at FROM users WHERE id = ?", (user["user_id"],)
                ).fetchone()
                started_at = int(row["created_at"]) if row and row["created_at"] else int(time.time())
            except sqlite3.Error:
                started_at = int(time.time())
        days = max(0, (int(time.time()) - int(started_at)) / 86400.0)
    finally:
        conn.close()

    goals = [
        {
            "key": k,
            "label": GOAL_LABELS[k],
            "completed": bool(by_key.get(k)),
            "completed_at": by_key.get(k),
        }
        for k in ALL_GOALS
    ]
    completed_count = sum(1 for g in goals if g["completed"])
    hide = (
        bool(widget_dismissed_at)
        or completed_count >= len(ALL_GOALS)
        or days >= FIRST_WEEK_DAYS
    )
    return JSONResponse({
        "goals": goals,
        "completed_count": completed_count,
        "total": len(ALL_GOALS),
        "dismissed": bool(widget_dismissed_at),
        "days_since_signup": round(days, 1),
        "hide_widget": hide,
    })


async def mark_goal(request: Request, key: str):
    user = _require_user(request)
    if key not in ALL_GOALS:
        raise HTTPException(status_code=400, detail="Unknown goal key")
    conn = _connect()
    try:
        _set_goal(conn, user["user_id"], key)
    finally:
        conn.close()
    return JSONResponse({"key": key, "completed": True})


async def dismiss_widget(request: Request):
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        conn.execute(
            "UPDATE user_onboarding SET widget_dismissed_at = ? WHERE user_id = ?",
            (int(time.time()), user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"dismissed": True})


# ── Dashboard overlay tour ──────────────────────────────────────────────────
#
# After the user finishes the 5-step /onboarding flow and lands on
# /dashboards for the first time, a 5-step spotlight tour walks them
# through the main nav. The tour is one-shot: completing OR skipping it
# stamps user_onboarding so it never replays.
#
# Gating logic in _tour_should_show:
#   - user_onboarding.completed_at IS NOT NULL  (finished signup flow)
#   - tour_completed_at IS NULL
#   - tour_skipped = 0
#   - migration 171 columns present (defensive — older DBs fall through
#     to "false" rather than raising).


def _row_get(row: Optional[dict], key: str, default: Any = None) -> Any:
    if not row:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _tour_should_show(conn: sqlite3.Connection, user_id: int) -> bool:
    if not _table_exists(conn, "user_onboarding"):
        return False
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_onboarding)")}
    if "tour_completed_at" not in cols:
        # Migration 171 not applied yet — fail closed; we won't pop a tour
        # we can't track.
        return False
    row = conn.execute(
        "SELECT completed_at, tour_completed_at, tour_skipped "
        "FROM user_onboarding WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return False
    if not row["completed_at"]:
        return False  # haven't finished the 5-step flow yet
    if row["tour_completed_at"]:
        return False
    if int(row["tour_skipped"] or 0):
        return False
    return True


async def tour_state(request: Request):
    """Returns whether the dashboard overlay tour should auto-start."""
    user = _require_user(request)
    conn = _connect()
    try:
        return JSONResponse({
            "should_show": _tour_should_show(conn, user["user_id"]),
        })
    finally:
        conn.close()


async def tour_complete(request: Request):
    """Mark the tour finished. Idempotent — repeat calls keep the first ts."""
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_onboarding)")}
        if "tour_completed_at" not in cols:
            return JSONResponse({"ok": True, "noop": "migration_171_pending"})
        conn.execute(
            "UPDATE user_onboarding "
            "SET tour_completed_at = COALESCE(tour_completed_at, ?) "
            "WHERE user_id = ?",
            (int(time.time()), user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


async def tour_skip(request: Request):
    """Mark the tour skipped. Same idempotency contract as tour_complete."""
    user = _require_user(request)
    conn = _connect()
    try:
        _ensure_onboarding_row(conn, user["user_id"])
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(user_onboarding)")}
        if "tour_skipped" not in cols:
            return JSONResponse({"ok": True, "noop": "migration_171_pending"})
        conn.execute(
            "UPDATE user_onboarding "
            "SET tour_skipped = 1, "
            "    tour_skipped_at = COALESCE(tour_skipped_at, ?) "
            "WHERE user_id = ?",
            (int(time.time()), user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True})


# ── Admin metrics ───────────────────────────────────────────────────────────


def _compute_metrics(conn: sqlite3.Connection) -> dict:
    metrics: dict[str, Any] = {}
    if not _table_exists(conn, "user_onboarding"):
        return {"error": "user_onboarding table missing"}

    total_users = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE COALESCE(is_deleted, 0) = 0"
    ).fetchone()
    metrics["total_users"] = int((total_users or {"n": 0})["n"])

    state_row = conn.execute(
        "SELECT "
        "  SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed, "
        "  SUM(CASE WHEN dismissed = 1 THEN 1 ELSE 0 END) AS dismissed, "
        "  SUM(CASE WHEN completed_at IS NULL AND dismissed = 0 THEN 1 ELSE 0 END) AS in_flight, "
        "  COUNT(*) AS rows_total "
        "FROM user_onboarding"
    ).fetchone()
    completed = int(state_row["completed"] or 0)
    dismissed = int(state_row["dismissed"] or 0)
    rows_total = int(state_row["rows_total"] or 0)
    metrics["onboarding_completed"] = completed
    metrics["onboarding_dismissed"] = dismissed
    metrics["onboarding_in_flight"] = int(state_row["in_flight"] or 0)
    metrics["completion_pct"] = round(100.0 * completed / rows_total, 1) if rows_total else 0.0

    # Drop-off: group by highest step_completed for users who haven't completed.
    dropoff_rows = conn.execute(
        "SELECT step_completed, COUNT(*) AS n FROM user_onboarding "
        "WHERE completed_at IS NULL AND dismissed = 0 "
        "GROUP BY step_completed ORDER BY step_completed"
    ).fetchall()
    metrics["dropoff"] = [
        {"step": int(r["step_completed"]), "count": int(r["n"])}
        for r in dropoff_rows
    ]

    avg_row = conn.execute(
        "SELECT AVG(completed_at - started_at) AS avg_seconds "
        "FROM user_onboarding WHERE completed_at IS NOT NULL"
    ).fetchone()
    avg_seconds = float(avg_row["avg_seconds"] or 0)
    metrics["avg_time_to_complete_seconds"] = round(avg_seconds, 1)
    metrics["avg_time_to_complete_minutes"] = round(avg_seconds / 60.0, 2)

    # First-week goals completion
    if _table_exists(conn, "user_first_week_goals"):
        goal_rows = conn.execute(
            "SELECT goal_key, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
            "FROM user_first_week_goals GROUP BY goal_key"
        ).fetchall()
        metrics["goals"] = {
            r["goal_key"]: {
                "total": int(r["total"] or 0),
                "done": int(r["done"] or 0),
            }
            for r in goal_rows
        }
        all_goals_row = conn.execute(
            """
            SELECT COUNT(*) AS users_hit_all
            FROM (
                SELECT user_id, COUNT(*) AS n
                FROM user_first_week_goals
                WHERE completed_at IS NOT NULL
                GROUP BY user_id
                HAVING n >= ?
            )
            """,
            (len(ALL_GOALS),),
        ).fetchone()
        metrics["users_hit_all_goals"] = int(all_goals_row["users_hit_all"] or 0)
    else:
        metrics["goals"] = {}
        metrics["users_hit_all_goals"] = 0

    return metrics


async def admin_metrics_json(request: Request):
    _require_admin(request)
    conn = _connect()
    try:
        return JSONResponse(_compute_metrics(conn))
    finally:
        conn.close()


async def admin_onboarding_page(request: Request):
    admin = _require_admin(request)
    conn = _connect()
    try:
        metrics = _compute_metrics(conn)
    finally:
        conn.close()

    goals_rows = ""
    for key in ALL_GOALS:
        entry = (metrics.get("goals") or {}).get(key, {"done": 0, "total": 0})
        pct = round(100.0 * entry["done"] / entry["total"], 1) if entry["total"] else 0.0
        goals_rows += (
            f"<tr><td>{_html.escape(GOAL_LABELS[key])}</td>"
            f"<td class='r mono'>{entry['done']} / {entry['total']}</td>"
            f"<td class='r mono'>{pct}%</td></tr>"
        )
    goals_rows = goals_rows or "<tr><td colspan='3' class='muted'>No goal data yet.</td></tr>"

    dropoff_rows = ""
    step_labels = {
        0: "Welcome screen (not advanced)",
        1: "After welcome",
        2: "After topic picker",
        3: "After source follow",
        4: "After notifications",
        5: "Complete",
    }
    for entry in metrics.get("dropoff") or []:
        label = step_labels.get(entry["step"], f"Step {entry['step']}")
        dropoff_rows += (
            f"<tr><td>{_html.escape(label)}</td>"
            f"<td class='r mono'>{entry['count']}</td></tr>"
        )
    dropoff_rows = dropoff_rows or "<tr><td colspan='2' class='muted'>No in-flight users.</td></tr>"

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Onboarding — narve.ai admin</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:32px;max-width:1040px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
letter-spacing:-0.02em;margin:0 0 8px}}
.meta{{color:var(--text-tertiary);font-size:12px;letter-spacing:0.08em;
text-transform:uppercase;margin-bottom:28px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
gap:12px;margin-bottom:28px}}
.card{{background:var(--bg-surface);border:1px solid var(--border-default);
border-radius:10px;padding:16px 20px}}
.label{{font-size:11px;text-transform:uppercase;letter-spacing:0.08em;
color:var(--text-secondary);margin-bottom:6px}}
.value{{font-family:var(--font-display);font-size:28px;font-weight:500}}
h2{{font-family:var(--font-display);font-size:22px;font-weight:500;
letter-spacing:-0.01em;margin:32px 0 12px}}
table{{width:100%;border-collapse:collapse;font-size:13px;
border:1px solid var(--border-default);border-radius:8px;overflow:hidden}}
th{{text-align:left;background:var(--bg-surface);color:var(--text-secondary);
padding:10px 14px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em}}
td{{padding:10px 14px;border-top:1px solid var(--border-default)}}
.r{{text-align:right}}
.mono{{font-family:var(--font-mono);font-size:12px}}
.muted{{color:var(--text-tertiary);text-align:center;padding:20px}}
</style></head><body>
<h1>Onboarding</h1>
<p class='meta'>Admin · {_html.escape(admin.get('email', ''))}</p>
<div class='cards'>
  <div class='card'><div class='label'>Users with onboarding rows</div>
    <div class='value'>{metrics.get('onboarding_completed', 0) + metrics.get('onboarding_dismissed', 0) + metrics.get('onboarding_in_flight', 0)}</div></div>
  <div class='card'><div class='label'>Tour completed</div>
    <div class='value'>{metrics.get('onboarding_completed', 0)}</div></div>
  <div class='card'><div class='label'>Tour completion %</div>
    <div class='value'>{metrics.get('completion_pct', 0)}%</div></div>
  <div class='card'><div class='label'>Tour skipped</div>
    <div class='value'>{metrics.get('onboarding_dismissed', 0)}</div></div>
  <div class='card'><div class='label'>Avg time to complete</div>
    <div class='value'>{metrics.get('avg_time_to_complete_minutes', 0)}m</div></div>
  <div class='card'><div class='label'>Hit all 6 first-week goals</div>
    <div class='value'>{metrics.get('users_hit_all_goals', 0)}</div></div>
</div>
<h2>First-week goals</h2>
<table><thead><tr><th>Goal</th><th class='r'>Users done</th><th class='r'>Rate</th></tr></thead>
<tbody>{goals_rows}</tbody></table>
<h2>Drop-off points (in-flight users)</h2>
<table><thead><tr><th>Step reached</th><th class='r'>Users stuck</th></tr></thead>
<tbody>{dropoff_rows}</tbody></table>
</body></html>"""
    return HTMLResponse(body)


# ── Registration ────────────────────────────────────────────────────────────


def register(app) -> None:
    # Onboarding flow
    app.add_api_route("/onboarding", onboarding_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/onboarding/advance", advance_step, methods=["POST"])
    app.add_api_route("/api/onboarding/dismiss", dismiss_tour, methods=["POST"])
    app.add_api_route("/api/onboarding/categories", save_categories, methods=["POST"])
    app.add_api_route("/api/onboarding/follow-sources", follow_sources, methods=["POST"])
    app.add_api_route("/api/onboarding/notifications", notifications_enabled, methods=["POST"])
    app.add_api_route("/api/onboarding/complete", complete_onboarding, methods=["POST"])
    app.add_api_route("/api/onboarding/suggested-sources", suggested_sources, methods=["GET"])
    app.add_api_route("/api/onboarding/sample-signal", sample_signal, methods=["GET"])

    # First-week goals
    app.add_api_route("/api/first-week/goals", goals_state, methods=["GET"])
    app.add_api_route("/api/first-week/goals/{key}", mark_goal, methods=["POST"])
    app.add_api_route("/api/first-week/widget/dismiss", dismiss_widget, methods=["POST"])

    # Aliases that match the spec's URL convention. Both shapes coexist
    # so older clients (sample-feed loader, etc.) keep working.
    app.add_api_route("/api/onboarding/goals", goals_state, methods=["GET"])
    app.add_api_route("/api/onboarding/goals/{key}", mark_goal, methods=["POST"])

    # Dashboard overlay tour.
    app.add_api_route("/api/onboarding/tour-state", tour_state, methods=["GET"])
    app.add_api_route("/api/onboarding/tour-complete", tour_complete, methods=["POST"])
    app.add_api_route("/api/onboarding/tour-skip", tour_skip, methods=["POST"])

    # Sample feed for empty dashboards
    app.add_api_route("/api/feed/sample", sample_feed, methods=["GET"])

    # Admin metrics
    app.add_api_route("/admin/onboarding", admin_onboarding_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/admin/api/onboarding/metrics", admin_metrics_json,
                      methods=["GET"])

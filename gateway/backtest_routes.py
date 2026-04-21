"""Backtest dashboard routes.

List / create / view runs + comparison view. Pro-gated, rate-limited to
5 runs per user per day.

Wire via ``backtest_routes.register(app)`` from server.py. Keeps state
access through its own sqlite3 handle — never imports db.py.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


log = logging.getLogger("backtest_routes")


MAX_RUNS_PER_USER_PER_DAY = 5


# ── DB ──────────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("GATEWAY_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else (Path(__file__).parent / p)
    return Path(__file__).parent / "auth.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── Lazy server imports ─────────────────────────────────────────────────────


def _deps():
    import server  # noqa: F401
    return server


def _require_pro_user(request: Request) -> dict:
    server = _deps()
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    # Pro gating — ``_user_plan_info`` lives in server.
    try:
        subs = {s["dashboard_key"]: s for s in _fetch_subs(user["user_id"])}
        plan_info = server._user_plan_info(user, subs, int(time.time()))
        plan = plan_info.get("plan") or "none"
    except Exception:
        plan = "none"
    if plan not in ("pro", "enterprise") and not user.get("is_admin"):
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


def _fetch_subs(user_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _runs_today(user_id: int) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM backtest_runs "
            "WHERE user_id = ? AND created_at >= ?",
            (user_id, int(time.time()) - 86400),
        ).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


# ── Handlers ────────────────────────────────────────────────────────────────


async def list_runs(request: Request):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 50",
            (user["user_id"],),
        ).fetchall()
        runs = [dict(r) for r in rows]
    finally:
        conn.close()
    return JSONResponse({"runs": runs})


async def create_run(
    request: Request,
    name: str = Form(...),
    params: str = Form(...),  # JSON string
):
    user = _require_pro_user(request)
    if _runs_today(user["user_id"]) >= MAX_RUNS_PER_USER_PER_DAY:
        raise HTTPException(status_code=429, detail="Daily backtest limit reached")
    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="params must be valid JSON")

    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO backtest_runs (user_id, name, params_json, status, created_at) "
            "VALUES (?,?,?,'queued',?)",
            (user["user_id"], name[:200], json.dumps(params_dict), int(time.time())),
        )
        run_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    # Enqueue via existing job backend.
    try:
        from jobs import enqueue_job
        await enqueue_job("run_backtest", run_id=run_id)
    except Exception as exc:
        log.warning("backtest enqueue failed: %s", exc)

    return JSONResponse({"run_id": run_id, "status": "queued"}, status_code=202)


async def get_run(request: Request, run_id: int):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM backtest_runs WHERE id = ? AND user_id = ?",
            (run_id, user["user_id"]),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    data = dict(row)
    if data.get("result_json"):
        try:
            data["result"] = json.loads(data["result_json"])
        except json.JSONDecodeError:
            data["result"] = None
    return JSONResponse(data)


async def save_comparison(
    request: Request,
    name: str = Form(...),
    run_ids: str = Form(...),
):
    user = _require_pro_user(request)
    try:
        ids = [int(x) for x in run_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="run_ids must be comma-separated ints")
    if not ids:
        raise HTTPException(status_code=400, detail="At least one run required")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO backtest_comparisons (user_id, name, run_ids_json, created_at) "
            "VALUES (?,?,?,?)",
            (user["user_id"], name[:200], json.dumps(ids), int(time.time())),
        )
        comparison_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"comparison_id": comparison_id}, status_code=201)


async def dashboard_page(request: Request):
    # Thin HTML shell — React-less; table rendered client-side by Chart.js.
    user = _require_pro_user(request)
    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Backtest — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:32px;max-width:960px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
letter-spacing:-0.02em;margin:0 0 24px}}
.card{{background:var(--bg-surface);border:1px solid var(--border-default);
border-radius:12px;padding:20px;margin:16px 0}}
.btn{{padding:10px 16px;background:var(--text-primary);
color:var(--interactive-text);border-radius:8px;border:none;
font-family:var(--font-ui);font-weight:500;cursor:pointer}}
</style></head><body>
<h1>Backtest</h1>
<p>Signed in as {user['email']}. Pro subscription active.</p>
<div class='card' id='runs'>Loading runs…</div>
<script src='/_gateway_static/charts.js?v=1' defer></script>
<script>
fetch('/api/backtest/runs').then(r=>r.json()).then(d=>{{
  const el = document.getElementById('runs');
  if (!d.runs || !d.runs.length) {{ el.textContent = 'No backtests yet.'; return; }}
  el.innerHTML = d.runs.map(r => `<div>${{r.name}} — ${{r.status}} · ROI ${{r.roi_pct ?? '—'}}%</div>`).join('');
}});
</script></body></html>"""
    return HTMLResponse(body)


# ── Registration ────────────────────────────────────────────────────────────


def register(app) -> None:
    app.add_api_route("/dashboard/backtest", dashboard_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/backtest/runs", list_runs, methods=["GET"])
    app.add_api_route("/api/backtest/runs", create_run, methods=["POST"])
    app.add_api_route("/api/backtest/runs/{run_id}", get_run, methods=["GET"])
    app.add_api_route("/api/backtest/comparisons", save_comparison, methods=["POST"])

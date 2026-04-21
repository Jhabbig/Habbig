"""Market-mover alerts routes — user alert rules + Feed-section endpoint.

  GET  /settings/alerts              HTML settings page (lists user rules)
  POST /api/alerts                   create rule
  PATCH /api/alerts/{id}             enable / disable / update thresholds
  DELETE /api/alerts/{id}            delete rule
  GET  /api/feed/movements           last 20 events (for Feed header)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("alerts_routes")


MAX_RULES_PER_USER = 10


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


def _require_user(request: Request) -> dict:
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    return user


async def list_rules(request: Request):
    user = _require_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM user_market_alerts WHERE user_id = ? ORDER BY created_at DESC",
            (user["user_id"],),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"rules": [dict(r) for r in rows]})


async def create_rule(
    request: Request,
    alert_type: str = Form(...),
    market_slug: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    min_movement_pct: float = Form(0.08),
    min_volume_multiple: float = Form(3.0),
    only_when_predictions_exist: int = Form(0),
    min_predictor_credibility: Optional[float] = Form(None),
):
    user = _require_user(request)
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM user_market_alerts WHERE user_id = ? AND is_active = 1",
            (user["user_id"],),
        ).fetchone()
        if existing and existing["n"] >= MAX_RULES_PER_USER:
            raise HTTPException(status_code=429, detail="Alert rule limit reached")
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO user_market_alerts ("
            "  user_id, alert_type, market_slug, category, min_movement_pct,"
            "  min_volume_multiple, only_when_predictions_exist,"
            "  min_predictor_credibility, is_active, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?, 1, ?, ?)",
            (
                user["user_id"], alert_type, market_slug, category,
                float(min_movement_pct), float(min_volume_multiple),
                int(bool(only_when_predictions_exist)),
                min_predictor_credibility,
                now, now,
            ),
        )
        conn.commit()
        return JSONResponse({"id": cur.lastrowid}, status_code=201)
    finally:
        conn.close()


async def update_rule(
    request: Request,
    rule_id: int,
    is_active: Optional[int] = Form(None),
    min_movement_pct: Optional[float] = Form(None),
    min_volume_multiple: Optional[float] = Form(None),
):
    user = _require_user(request)
    fields: list[str] = []
    args: list[Any] = []
    if is_active is not None:
        fields.append("is_active = ?"); args.append(int(bool(is_active)))
    if min_movement_pct is not None:
        fields.append("min_movement_pct = ?"); args.append(float(min_movement_pct))
    if min_volume_multiple is not None:
        fields.append("min_volume_multiple = ?"); args.append(float(min_volume_multiple))
    if not fields:
        return JSONResponse({"updated": False}, status_code=400)
    fields.append("updated_at = ?"); args.append(int(time.time()))
    args.extend([rule_id, user["user_id"]])

    conn = _connect()
    try:
        cur = conn.execute(
            f"UPDATE user_market_alerts SET {', '.join(fields)} "
            f"WHERE id = ? AND user_id = ?",
            tuple(args),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"updated": cur.rowcount > 0})


async def delete_rule(request: Request, rule_id: int):
    user = _require_user(request)
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM user_market_alerts WHERE id = ? AND user_id = ?",
            (rule_id, user["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"deleted": cur.rowcount > 0})


async def feed_movements(request: Request):
    _require_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM market_movement_events "
            "ORDER BY detected_at DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"events": [dict(r) for r in rows]})


async def settings_page(request: Request):
    user = _require_user(request)
    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Alert rules — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:720px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:40px;
letter-spacing:-0.02em;margin:0 0 20px}}
</style></head><body>
<h1>Market alert rules</h1>
<p>Signed in as {user['email']}. Max {MAX_RULES_PER_USER} active rules.</p>
<div id='rules'>Loading…</div>
<script>
fetch('/api/alerts').then(r=>r.json()).then(d=>{{
  const el = document.getElementById('rules');
  if (!d.rules || !d.rules.length) {{ el.textContent = 'No alert rules yet.'; return; }}
  el.innerHTML = d.rules.map(r => `<div>${{r.alert_type}} · ${{r.market_slug || r.category || 'all'}} · ${{r.is_active ? 'active' : 'off'}}</div>`).join('');
}});
</script></body></html>"""
    return HTMLResponse(body)


def register(app) -> None:
    app.add_api_route("/settings/alerts", settings_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/alerts", list_rules, methods=["GET"])
    app.add_api_route("/api/alerts", create_rule, methods=["POST"])
    app.add_api_route("/api/alerts/{rule_id}", update_rule, methods=["PATCH"])
    app.add_api_route("/api/alerts/{rule_id}", delete_rule, methods=["DELETE"])
    app.add_api_route("/api/feed/movements", feed_movements, methods=["GET"])

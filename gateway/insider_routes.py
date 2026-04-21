"""Insider signals dashboard (Pro only).

Tabs + sub-views:
  /dashboard/insider                  main list page
  /api/insider/signals                recent signals (paginated)
  /api/insider/markets/<slug>         correlations for one market
  /api/insider/leaderboard            top insiders by rows / volume

Heavy HTML rendering deferred to client-side JS — server emits JSON +
a thin shell. Every response includes the mandatory legal disclaimer.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("insider_routes")


LEGAL_DISCLAIMER = (
    "All data derived from mandatory public disclosures. "
    "narve.ai does not possess non-public information."
)


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


def _require_pro_user(request: Request) -> dict:
    import server
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    try:
        subs_rows = []
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user["user_id"],),
            ).fetchall()
            subs_rows = [dict(r) for r in rows]
        finally:
            conn.close()
        subs = {s["dashboard_key"]: s for s in subs_rows}
        import time as _t
        plan = (server._user_plan_info(user, subs, int(_t.time())).get("plan") or "none")
    except Exception:
        plan = "none"
    if plan not in ("pro", "enterprise") and not user.get("is_admin"):
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


# ── Read endpoints ──────────────────────────────────────────────────────────


async def signals_list(request: Request):
    _require_pro_user(request)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit") or "50")))
    except ValueError:
        limit = 50
    source = request.query_params.get("source")
    strength = request.query_params.get("strength")
    days = request.query_params.get("days") or "30"
    try:
        days_i = max(1, min(365, int(days)))
    except ValueError:
        days_i = 30

    import time as _t
    since = int(_t.time()) - days_i * 86400
    clauses = ["disclosed_at >= ?"]
    args: list[Any] = [since]
    if source:
        clauses.append("source = ?"); args.append(source)
    if strength:
        clauses.append("signal_strength = ?"); args.append(strength)

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM insider_signals WHERE " + " AND ".join(clauses)
            + " ORDER BY disclosed_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({
        "signals": [dict(r) for r in rows],
        "disclaimer": LEGAL_DISCLAIMER,
    })


async def market_correlations(request: Request, market_slug: str):
    _require_pro_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT c.*, s.actor_name, s.source AS signal_source, "
            "       s.signal_strength, s.disclosed_at "
            "FROM insider_market_correlations c "
            "JOIN insider_signals s ON s.id = c.signal_id "
            "WHERE c.market_slug = ? "
            "ORDER BY c.insider_score DESC LIMIT 50",
            (market_slug,),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({
        "market_slug": market_slug,
        "correlations": [dict(r) for r in rows],
        "disclaimer": LEGAL_DISCLAIMER,
    })


async def leaderboard(request: Request):
    _require_pro_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT actor_name, source, COUNT(*) AS signal_count, "
            "       SUM(COALESCE(amount_usd, 0)) AS total_amount "
            "FROM insider_signals "
            "GROUP BY actor_name, source "
            "ORDER BY signal_count DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({
        "leaderboard": [dict(r) for r in rows],
        "disclaimer": LEGAL_DISCLAIMER,
    })


async def dashboard_page(request: Request):
    user = _require_pro_user(request)
    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Insider — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:960px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:48px;
margin:0 0 8px;letter-spacing:-0.02em}}
.meta{{color:var(--text-tertiary);font-size:12px;font-family:var(--font-mono);
text-transform:uppercase;letter-spacing:0.1em;margin-bottom:24px}}
.disclaimer{{font-size:12px;color:var(--text-tertiary);border-top:1px solid
var(--border-default);padding-top:16px;margin-top:48px}}
</style></head><body>
<h1>Insider signals</h1>
<p class='meta'>Pro · Congressional trades · Form 4 · 13F · Options flow · FEC · Lobbying</p>
<div id='signals'>Loading…</div>
<p class='disclaimer'>{LEGAL_DISCLAIMER}</p>
<script>
fetch('/api/insider/signals?limit=50').then(r=>r.json()).then(d=>{{
  const el = document.getElementById('signals');
  if (!d.signals || !d.signals.length) {{ el.textContent = 'No signals in the window.'; return; }}
  el.innerHTML = d.signals.map(s => `<div>${{s.disclosed_at}} · <strong>${{s.source}}</strong> · ${{s.actor_name}} · ${{s.ticker || ''}} · ${{s.signal_strength}}</div>`).join('');
}});
</script>
</body></html>"""
    return HTMLResponse(body)


def register(app) -> None:
    app.add_api_route("/dashboard/insider", dashboard_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/insider/signals", signals_list, methods=["GET"])
    app.add_api_route("/api/insider/markets/{market_slug}", market_correlations, methods=["GET"])
    app.add_api_route("/api/insider/leaderboard", leaderboard, methods=["GET"])

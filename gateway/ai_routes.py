"""Public AI-feature routes — source summary + admin usage snapshot.

  GET /api/sources/{handle}/summary       plain-English summary (public)
  GET /admin/api/ai/usage                 per-day per-feature rollup (admin)
  GET /admin/ai-usage                     HTML dashboard (admin, read-only)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from ai import source_summariser


log = logging.getLogger("ai_routes")


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


async def source_summary(request: Request, handle: str):
    summary = await source_summariser.generate_source_summary(handle)
    return JSONResponse(summary)


async def admin_ai_usage(request: Request):
    import server
    user = server._require_admin_user(request)
    try:
        days = max(1, min(90, int(request.query_params.get("days") or "14")))
    except ValueError:
        days = 14
    start = int(time.time()) - days * 86400
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                feature,
                COUNT(*) AS calls,
                SUM(cached_hit) AS cache_hits,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE timestamp >= ?
            GROUP BY day, feature
            ORDER BY day DESC, feature ASC
            """,
            (start,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"claude_usage_log read failed: {exc}")
    finally:
        conn.close()
    return JSONResponse({
        "days": days,
        "rollup": [dict(r) for r in rows],
        "admin": user["email"],
    })


async def admin_ai_usage_page(request: Request):
    """Read-only HTML dashboard — admin-only.

    Renders a 14-day rollup of per-feature Claude spend + cache hit rate.
    Lives in ai_routes.py rather than server.py so the prompt's
    no-server.py constraint is respected.
    """
    import html as _html
    import server
    user = server._require_admin_user(request)

    try:
        days = max(1, min(90, int(request.query_params.get("days") or "14")))
    except ValueError:
        days = 14
    start = int(time.time()) - days * 86400
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m-%d', timestamp, 'unixepoch') AS day,
                feature,
                COUNT(*) AS calls,
                SUM(cached_hit) AS cache_hits,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
            FROM claude_usage_log
            WHERE timestamp >= ?
            GROUP BY day, feature
            ORDER BY day DESC, feature ASC
            """,
            (start,),
        ).fetchall()
        total_row = conn.execute(
            "SELECT SUM(cost_usd) AS cost, COUNT(*) AS calls, "
            "       SUM(cached_hit) AS hits "
            "FROM claude_usage_log WHERE timestamp >= ?",
            (start,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"claude_usage_log read failed: {exc}")
    finally:
        conn.close()

    total_cost = float(total_row["cost"] or 0) if total_row else 0.0
    total_calls = int(total_row["calls"] or 0) if total_row else 0
    total_hits = int(total_row["hits"] or 0) if total_row else 0
    hit_rate = round(100 * total_hits / total_calls, 1) if total_calls else 0.0

    table_rows = "".join(
        f"<tr><td class='mono'>{_html.escape(r['day'])}</td>"
        f"<td>{_html.escape(r['feature'])}</td>"
        f"<td class='r'>{r['calls']}</td>"
        f"<td class='r'>{r['cache_hits']}</td>"
        f"<td class='r mono'>{r['input_tokens']}/{r['output_tokens']}</td>"
        f"<td class='r mono'>${r['cost_usd']:.4f}</td></tr>"
        for r in rows
    ) or "<tr><td colspan='6' style='text-align:center;color:var(--text-tertiary)'>No calls yet.</td></tr>"

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>AI usage — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:32px;max-width:1080px;margin:0 auto}}
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
table{{width:100%;border-collapse:collapse;font-size:13px;
border:1px solid var(--border-default);border-radius:8px;overflow:hidden}}
th{{text-align:left;background:var(--bg-surface);color:var(--text-secondary);
padding:10px 14px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em}}
td{{padding:10px 14px;border-top:1px solid var(--border-default)}}
.r{{text-align:right}}
.mono{{font-family:var(--font-mono);font-size:12px}}
</style></head><body>
<h1>AI usage</h1>
<p class='meta'>Admin · {user['email']} · {days}-day window</p>
<div class='cards'>
  <div class='card'><div class='label'>Total spend</div>
    <div class='value'>${total_cost:.2f}</div></div>
  <div class='card'><div class='label'>Calls</div>
    <div class='value'>{total_calls}</div></div>
  <div class='card'><div class='label'>Cache hit rate</div>
    <div class='value'>{hit_rate}%</div></div>
  <div class='card'><div class='label'>Window</div>
    <div class='value'>{days} d</div></div>
</div>
<table>
<thead><tr><th>Day</th><th>Feature</th><th class='r'>Calls</th>
<th class='r'>Cache hits</th><th class='r'>Tokens (in/out)</th>
<th class='r'>Cost</th></tr></thead>
<tbody>{table_rows}</tbody>
</table>
</body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(body)


def register(app) -> None:
    app.add_api_route("/api/sources/{handle}/summary", source_summary, methods=["GET"])
    app.add_api_route("/admin/api/ai/usage", admin_ai_usage, methods=["GET"])
    from fastapi.responses import HTMLResponse
    app.add_api_route(
        "/admin/ai-usage", admin_ai_usage_page,
        methods=["GET"], response_class=HTMLResponse,
        include_in_schema=False,
    )

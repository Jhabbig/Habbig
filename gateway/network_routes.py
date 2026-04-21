"""Source network sub-view — echo chambers + most-independent sources.

Wire via ``network_routes.register(app)``. Public read-only surface:

  GET /sources/network         HTML page — echo clusters + independents
  GET /api/sources/network     JSON of the most recent snapshot
  GET /api/sources/{h}/pairs   relationships for one handle
"""

from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("network_routes")


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


async def network_page(request: Request):
    conn = _connect()
    try:
        snap = conn.execute(
            "SELECT * FROM source_networks ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        snap = None
    finally:
        conn.close()

    clusters: list[list[str]] = []
    indie: list[dict] = []
    if snap:
        try:
            clusters = json.loads(snap["echo_chamber_clusters"] or "[]")
            indie = json.loads(snap["most_independent_sources"] or "[]")
        except json.JSONDecodeError:
            pass

    clusters_html = "".join(
        "<li>" + " · ".join(f"<span class='mono'>@{html.escape(h)}</span>" for h in c) + "</li>"
        for c in clusters
    ) or "<li>No echo-chamber clusters detected.</li>"
    indie_html = "".join(
        f"<li><span class='mono'>@{html.escape(s['handle'])}</span> · "
        f"independence {float(s.get('score') or 0):.3f}</li>"
        for s in indie
    ) or "<li>No independence scoring data yet.</li>"

    body = f"""<!DOCTYPE html><html><head>
<meta charset='utf-8'><title>Source network — narve.ai</title>
<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>
<style>body{{background:var(--bg-base);color:var(--text-primary);
font-family:var(--font-ui);padding:40px;max-width:760px;margin:0 auto}}
h1{{font-family:var(--font-display);font-style:italic;font-size:48px;
letter-spacing:-0.02em;margin:0 0 24px}}
h2{{font-size:14px;text-transform:uppercase;letter-spacing:0.08em;
color:var(--text-secondary);margin:32px 0 12px}}
.mono{{font-family:var(--font-mono);font-size:12px}}
ul{{list-style:none;padding:0}}
li{{padding:10px 14px;background:var(--bg-surface);
border:1px solid var(--border-default);border-radius:8px;margin:8px 0}}
</style></head><body>
<h1>Source network</h1>
<p>Snapshot from the weekly network-analysis job.</p>
<h2>Echo-chamber clusters</h2><ul>{clusters_html}</ul>
<h2>Most independent sources</h2><ul>{indie_html}</ul>
</body></html>"""
    return HTMLResponse(body)


async def network_json(request: Request):
    conn = _connect()
    try:
        snap = conn.execute(
            "SELECT * FROM source_networks ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not snap:
        return JSONResponse({"snapshot": None})
    row = dict(snap)
    row["echo_chamber_clusters"] = json.loads(row.get("echo_chamber_clusters") or "[]")
    row["most_independent_sources"] = json.loads(row.get("most_independent_sources") or "[]")
    return JSONResponse({"snapshot": row})


async def handle_pairs(request: Request, handle: str):
    handle = handle.strip().lstrip("@")
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM source_relationships "
            "WHERE source_a = ? OR source_b = ? "
            "ORDER BY markets_both_predicted DESC LIMIT 50",
            (handle, handle),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse({"handle": handle, "pairs": [dict(r) for r in rows]})


def register(app) -> None:
    app.add_api_route("/sources/network", network_page,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/api/sources/network", network_json, methods=["GET"])
    app.add_api_route("/api/sources/{handle}/pairs", handle_pairs, methods=["GET"])

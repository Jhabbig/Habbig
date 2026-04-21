"""Weekly-report viewer + PDF download (Pro).

  GET /reports/weekly              list of the user's past reports
  GET /reports/weekly/{id}/pdf     PDF download (if rendered)
  GET /reports/weekly/{id}         HTML preview (excerpt stored in DB)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


log = logging.getLogger("reports_routes")


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
    import server, time as _t
    user = server.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Auth required")
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE user_id = ?", (user["user_id"],),
            ).fetchall()
            subs = {r["dashboard_key"]: dict(r) for r in rows}
        finally:
            conn.close()
        plan = server._user_plan_info(user, subs, int(_t.time())).get("plan") or "none"
    except Exception:
        plan = "none"
    if plan not in ("pro", "enterprise") and not user.get("is_admin"):
        raise HTTPException(status_code=402, detail="Pro subscription required")
    return user


async def list_reports(request: Request):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, period_start, period_end, status, completed_at "
            "FROM weekly_reports WHERE user_id = ? "
            "ORDER BY period_start DESC LIMIT 52",
            (user["user_id"],),
        ).fetchall()
    finally:
        conn.close()
    body = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Weekly reports — narve.ai</title>"
        "<link rel='stylesheet' href='/_gateway_static/gateway.css?v=8'>"
        "<style>body{background:var(--bg-base);color:var(--text-primary);"
        "font-family:var(--font-ui);padding:40px;max-width:720px;margin:0 auto}"
        "h1{font-family:var(--font-display);font-style:italic;font-size:40px;margin:0 0 24px}"
        "table{width:100%;border-collapse:collapse}"
        "td{padding:10px 0;border-bottom:1px solid var(--border-default)}"
        "</style></head><body><h1>Weekly reports</h1><table>"
    )
    for r in rows:
        import datetime as _dt
        start = _dt.datetime.utcfromtimestamp(int(r["period_start"])).strftime("%d %b %Y")
        end = _dt.datetime.utcfromtimestamp(int(r["period_end"])).strftime("%d %b %Y")
        body += (
            f"<tr><td>{start} – {end}</td><td>{r['status']}</td>"
            f"<td><a href='/reports/weekly/{r['id']}'>view</a> · "
            f"<a href='/reports/weekly/{r['id']}/pdf'>pdf</a></td></tr>"
        )
    body += "</table></body></html>"
    return HTMLResponse(body)


async def view_report(request: Request, report_id: int):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM weekly_reports WHERE id = ? AND user_id = ?",
            (report_id, user["user_id"]),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    html = row["html_excerpt"] or f"<p>Report {report_id} — {row['status']}</p>"
    return HTMLResponse(html)


async def pdf_report(request: Request, report_id: int):
    user = _require_pro_user(request)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT pdf_path FROM weekly_reports WHERE id = ? AND user_id = ?",
            (report_id, user["user_id"]),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["pdf_path"]:
        raise HTTPException(status_code=404, detail="PDF not rendered")
    path = Path(row["pdf_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="PDF file missing")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        filename=f"narve-weekly-{report_id}.pdf",
    )


def register(app) -> None:
    app.add_api_route("/reports/weekly", list_reports,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/reports/weekly/{report_id}", view_report,
                      methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)
    app.add_api_route("/reports/weekly/{report_id}/pdf", pdf_report,
                      methods=["GET"], include_in_schema=False)

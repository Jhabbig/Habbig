"""Public status page and admin incident management routes.

Loaded at the end of server.py (same pattern as server_features.py).
Nothing here imports server at module-top — we pull `app`, `render_page`,
`current_user`, etc. from the `server` module lazily so the file can be
reloaded without double-registering routes.

Endpoints:

    GET  /status                 → public HTML page
    GET  /api/status             → public JSON snapshot
    GET  /status/feed.xml        → RSS 2.0 feed
    POST /api/status/subscribe   → add an email subscription
    POST /api/status/unsubscribe → revoke via signed-random token
    GET  /status/unsubscribe     → one-click unsubscribe landing page
    GET  /admin/status           → admin dashboard (incidents + subs)
    POST /admin/incidents        → create incident
    POST /admin/incidents/{id}   → update (status/title/severity/components)
    POST /admin/incidents/{id}/updates → append timeline entry
    POST /admin/incidents/{id}/resolve → mark resolved (convenience)
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import html as _html
import json
import logging
import os
import re
import time
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import server
from server import (
    app,
    render_page,
    _require_admin_user,
    _denied_response,
    _role_badge,
)

from status_system import (
    COMPONENTS,
    COMPONENT_KEYS,
    COMPONENT_DISPLAY,
    INCIDENT_STATES,
    SEVERITIES,
)
from status_system import db as status_db
from status_system import feeds as status_feeds
from status_system import subscriptions as status_subs
from status_system import uptime as status_uptime


log = logging.getLogger("status.routes")


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _base_url(request: Request) -> str:
    """Return the scheme://host for the current request. Falls back to
    APP_URL env var for out-of-band callers (e.g. RSS generation during
    a cron firing)."""
    try:
        host = request.headers.get("host", "")
        if host:
            scheme = "https" if request.url.scheme == "https" else "http"
            if os.environ.get("PRODUCTION", "").lower() in ("1", "true"):
                scheme = "https"
            return f"{scheme}://{host}"
    except Exception:
        pass
    return os.environ.get("APP_URL", "https://narve.ai").rstrip("/")


# ── helpers ─────────────────────────────────────────────────────────────


def _status_dot(status: str) -> str:
    """Small monochrome HTML dot for the component rows. The spec
    explicitly bans red/green/yellow — text labels carry the meaning.
    """
    cls = {
        "operational": "status-dot status-dot-ok",
        "degraded": "status-dot status-dot-deg",
        "outage": "status-dot status-dot-out",
    }.get(status, "status-dot")
    return f'<span class="{cls}" aria-hidden="true"></span>'


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "n/a"
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%b %-d, %Y %H:%M UTC")


def _rel_ts(ts: Optional[int]) -> str:
    """Relative timestamp for the 'Last checked 18 seconds ago' banner."""
    if not ts:
        return "never"
    now = int(time.time())
    delta = max(0, now - int(ts))
    if delta < 60:
        return f"{delta} seconds ago"
    if delta < 3600:
        return f"{delta // 60} minutes ago"
    if delta < 86400:
        return f"{delta // 3600} hours ago"
    return f"{delta // 86400} days ago"


def _component_rows_html(system: dict) -> str:
    parts: list[str] = []
    for key, display in COMPONENTS:
        info = system["components"].get(key, {})
        status = info.get("status") or "operational"
        label = status.title()
        parts.append(
            '<div class="status-row">'
            f'<div class="status-row-label">{_html.escape(display)}</div>'
            f'<div class="status-row-state">{_status_dot(status)}'
            f'<span class="status-row-status">{_html.escape(label)}</span></div>'
            "</div>"
        )
    return "\n".join(parts)


def _uptime_bars_html(overall: dict) -> str:
    """Render the 90-day uptime visualisation as a row of divs.

    Each day is a `<div class="uptime-day" style="--fill:0.xx"
    data-date="…" data-status="…">`. CSS controls the height.
    """
    out: list[str] = []
    for d in overall["daily_rollup"]:
        pct = d["uptime_pct"]
        status = d["status"]
        if pct is None:
            fill = 0.0
            title = f"{d['date']} — no data"
        else:
            fill = max(0.0, min(1.0, pct / 100.0))
            title = f"{d['date']} — {pct:.2f}% · {status}"
        out.append(
            f'<div class="uptime-day" data-date="{_html.escape(d["date"])}" '
            f'data-status="{_html.escape(status)}" '
            f'style="--fill:{fill:.3f}" title="{_html.escape(title)}"></div>'
        )
    return "\n".join(out)


def _incident_list_html(incidents: list[dict]) -> str:
    if not incidents:
        return '<p class="status-empty">No incidents in the last 90 days.</p>'
    rows: list[str] = []
    for inc in incidents:
        created = _dt.datetime.fromtimestamp(inc["created_at"], tz=_dt.timezone.utc)
        date_str = created.strftime("%b %-d, %Y")
        if inc["resolved_at"]:
            dur_min = max(1, (inc["resolved_at"] - inc["created_at"]) // 60)
            if dur_min >= 60:
                dur_str = f"Resolved {dur_min // 60}h {dur_min % 60}m"
            else:
                dur_str = f"Resolved {dur_min}m"
        else:
            dur_str = f"Ongoing · {inc['status'].title()}"
        comps = ", ".join(
            COMPONENT_DISPLAY.get(c, c) for c in inc["affected_components"]
        ) or "n/a"
        rows.append(
            f'<article class="incident-item" id="incident-{inc["id"]}">'
            f'<header class="incident-head">'
            f'<span class="incident-date">{_html.escape(date_str)}</span>'
            f'<span class="incident-sep">—</span>'
            f'<span class="incident-title">{_html.escape(inc["title"])}</span>'
            f"</header>"
            f'<div class="incident-meta">{_html.escape(dur_str)}'
            f'<span class="incident-meta-sep"> · </span>'
            f'Severity: {_html.escape(inc["severity"])}'
            f'<span class="incident-meta-sep"> · </span>'
            f'{_html.escape(comps)}</div>'
            f'<p class="incident-desc">{_html.escape(inc["description"] or "")}</p>'
            "</article>"
        )
    return "\n".join(rows)


def _assemble_status_context(request: Request) -> dict:
    system = status_uptime.overall_system_status()
    overall = status_uptime.compute_overall_uptime_last_n_days(90)
    recent = status_db.list_recent_incidents(limit=20)

    return {
        "status_message": system["message"],
        "status_level": system["status"],
        "last_checked_rel": _rel_ts(system["last_checked_ts"]),
        "last_checked_abs": _fmt_ts(system["last_checked_ts"]),
        "raw_component_rows": _component_rows_html(system),
        "raw_uptime_bars": _uptime_bars_html(overall),
        "overall_uptime_pct": f"{overall['uptime_pct']:.2f}",
        "incident_count_window": str(overall["incidents"]),
        "raw_incident_list": _incident_list_html(recent),
        "feed_url": f"{_base_url(request)}/status/feed.xml",
    }


# ── public routes ───────────────────────────────────────────────────────


@app.get("/status", response_class=HTMLResponse, include_in_schema=False)
async def status_page(request: Request):
    """Public status page. No auth, no gate — accessible to anyone."""
    ctx = _assemble_status_context(request)
    return render_page("status", request=request, **ctx)


@app.get("/api/status", include_in_schema=False)
async def api_status(request: Request):
    """Public JSON snapshot — same data the HTML page renders from."""
    system = status_uptime.overall_system_status()
    overall = status_uptime.compute_overall_uptime_last_n_days(90)
    recent = status_db.list_recent_incidents(limit=20)

    return JSONResponse(
        {
            "status": system["status"],
            "message": system["message"],
            "last_checked_ts": system["last_checked_ts"],
            "components": [
                {
                    "key": k,
                    "display_name": display,
                    "status": system["components"].get(k, {}).get("status", "operational"),
                    "response_time_ms": system["components"].get(k, {}).get("response_time_ms"),
                    "checked_at": system["components"].get(k, {}).get("checked_at"),
                }
                for k, display in COMPONENTS
            ],
            "uptime_90d": {
                "uptime_pct": overall["uptime_pct"],
                "downtime_minutes": overall["downtime_minutes"],
                "total_minutes": overall["total_minutes"],
                "incidents": overall["incidents"],
                "daily": overall["daily_rollup"],
            },
            "recent_incidents": [
                {
                    "id": i["id"],
                    "created_at": i["created_at"],
                    "resolved_at": i["resolved_at"],
                    "severity": i["severity"],
                    "affected_components": i["affected_components"],
                    "title": i["title"],
                    "status": i["status"],
                    "description": i["description"],
                }
                for i in recent
            ],
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/status/feed.xml", include_in_schema=False)
async def status_feed(request: Request):
    xml = status_feeds.build_rss_feed(base_url=_base_url(request), limit=50)
    return Response(
        content=xml,
        media_type="application/rss+xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.post("/api/status/subscribe", include_in_schema=False)
async def api_status_subscribe(request: Request):
    body = await _read_json(request)
    email = (body.get("email") or "").strip().lower()
    components = body.get("components") or "all"

    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid email")

    if isinstance(components, list):
        for c in components:
            if c not in COMPONENT_KEYS:
                raise HTTPException(status_code=400, detail=f"unknown component: {c}")
    elif components != "all":
        raise HTTPException(status_code=400, detail="components must be 'all' or a list")

    try:
        sub = status_db.create_subscription(email, components)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({
        "ok": True,
        "email": sub["email"],
        "status": sub["status"],
        "unsubscribe_url": f"{_base_url(request)}/status/unsubscribe?token={sub['token']}",
    })


@app.post("/api/status/unsubscribe", include_in_schema=False)
async def api_status_unsubscribe(request: Request):
    body = await _read_json(request)
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="missing token")
    ok = status_db.delete_subscription_by_token(token)
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    return JSONResponse({"ok": True})


@app.get("/status/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
async def status_unsubscribe_landing(request: Request, token: str = ""):
    """One-click unsubscribe landing page. Marks the subscription
    cancelled and renders a confirmation."""
    if not token:
        return HTMLResponse(
            "<h1>Missing token</h1><p>The unsubscribe link is incomplete.</p>",
            status_code=400,
        )
    sub = status_db.get_subscription_by_token(token)
    if not sub:
        return HTMLResponse(
            "<h1>Already unsubscribed</h1><p>This link is no longer active.</p>",
            status_code=404,
        )
    status_db.delete_subscription_by_token(token)
    email_esc = _html.escape(sub["email"])
    return HTMLResponse(
        f"<!DOCTYPE html><html><head><title>Unsubscribed · narve.ai</title>"
        f'<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f'<link rel="stylesheet" href="/_gateway_static/gateway.css"></head>'
        f"<body><main style='max-width:520px;margin:80px auto;padding:0 24px;font-family:inherit'>"
        f"<h1 style='margin:0 0 12px'>You're unsubscribed</h1>"
        f"<p style='color:var(--text-secondary,#6b7280)'>No more status updates will be sent to "
        f"<strong>{email_esc}</strong>.</p>"
        f'<p style="margin-top:32px"><a href="/status">← Back to status page</a></p>'
        f"</main></body></html>"
    )


async def _read_json(request: Request) -> dict:
    """Parse JSON body, also accepting urlencoded form posts as a fallback."""
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    try:
        form = await request.form()
        return {k: v for k, v in form.items()}
    except Exception:
        return {}


# ── admin routes ────────────────────────────────────────────────────────


@app.get("/admin/status", response_class=HTMLResponse, include_in_schema=False)
async def admin_status_page(request: Request):
    user = _require_admin_user(request, page=True)
    # _require_admin_user can return None (non-admin → render 403),
    # a RedirectResponse (2FA pending → hand it back untouched), or a
    # dict (admin ok). Guard against the non-dict cases.
    if user is None:
        return _denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse — hand straight back to Starlette

    open_incidents = status_db.list_open_incidents()
    recent = status_db.list_recent_incidents(limit=30)
    subs = status_db.list_all_subscriptions()

    return render_page(
        "admin_status",
        request=request,
        username=user.get("username") or user.get("email", ""),
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_open_incidents=_admin_incident_rows(open_incidents, request),
        raw_all_incidents=_admin_incident_rows(recent, request, include_resolved=True),
        raw_subscriber_list=_admin_subscriber_rows(subs),
        raw_component_checkboxes=_admin_component_checkboxes(),
        raw_severity_options=_admin_severity_options(),
        raw_status_options=_admin_status_options(),
        open_count=str(len(open_incidents)),
        total_subs=str(len(subs)),
    )


def _admin_component_checkboxes() -> str:
    return "\n".join(
        f'<label class="admin-check"><input type="checkbox" name="components" value="{k}"> '
        f'{_html.escape(display)}</label>'
        for k, display in COMPONENTS
    )


def _admin_severity_options() -> str:
    return "\n".join(
        f'<option value="{s}">{s.title()}</option>' for s in SEVERITIES
    )


def _admin_status_options() -> str:
    return "\n".join(
        f'<option value="{s}">{s.title()}</option>' for s in INCIDENT_STATES
    )


def _admin_incident_rows(incidents: list[dict], request: Request, *, include_resolved: bool = False) -> str:
    if not incidents:
        return '<p class="status-empty">None.</p>'
    out: list[str] = []
    for inc in incidents:
        resolved_label = (
            f'<span class="badge">Resolved {_fmt_ts(inc["resolved_at"])}</span>'
            if inc["resolved_at"] else f'<span class="badge">{_html.escape(inc["status"])}</span>'
        )
        origin_label = ' <span class="badge">auto</span>' if inc["auto_created"] else ""
        comps = ", ".join(COMPONENT_DISPLAY.get(c, c) for c in inc["affected_components"]) or "n/a"
        update_form = (
            f'<form method="post" action="/admin/incidents/{inc["id"]}/updates" class="admin-inline">'
            f'<select name="status">'
            + "".join(f'<option value="{s}">{s.title()}</option>' for s in INCIDENT_STATES)
            + '</select>'
            '<input type="text" name="message" placeholder="Update message" required>'
            '<button type="submit" class="btn btn-primary-outline">Post update</button>'
            "</form>"
        )
        resolve_btn = (
            f'<form method="post" action="/admin/incidents/{inc["id"]}/resolve" class="admin-inline">'
            '<button type="submit" class="btn">Mark resolved</button></form>'
            if not inc["resolved_at"] else ""
        )
        out.append(
            f'<article class="admin-incident" id="incident-{inc["id"]}">'
            f'<h3>{_html.escape(inc["title"])} '
            f'<small>#{inc["id"]}</small>{origin_label}</h3>'
            f'<div class="admin-incident-meta">{resolved_label}'
            f' · Severity {_html.escape(inc["severity"])}'
            f' · Affected: {_html.escape(comps)}'
            f' · Created {_fmt_ts(inc["created_at"])}</div>'
            f'<p>{_html.escape(inc["description"] or "")}</p>'
            f'{update_form}'
            f'{resolve_btn}'
            "</article>"
        )
    return "\n".join(out)


def _admin_subscriber_rows(subs: list[dict]) -> str:
    if not subs:
        return '<p class="status-empty">No subscribers yet.</p>'
    rows = [
        '<table class="admin-subs"><thead><tr>'
        '<th>Email</th><th>Components</th><th>Subscribed</th></tr></thead><tbody>'
    ]
    for s in subs:
        comps = s["components"]
        comps_str = "all" if comps == "all" else ", ".join(comps)
        rows.append(
            "<tr>"
            f"<td>{_html.escape(s['email'])}</td>"
            f"<td>{_html.escape(comps_str)}</td>"
            f"<td>{_fmt_ts(s['subscribed_at'])}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


@app.post("/admin/incidents", include_in_schema=False)
async def admin_create_incident(request: Request):
    user = _require_admin_user(request)
    # _require_admin_user may return a RedirectResponse for 2FA-pending
    # sessions instead of raising HTTPException. Forward it verbatim.
    if not isinstance(user, dict):
        return user
    form = await request.form()

    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip()
    severity = (form.get("severity") or "minor").strip()
    status = (form.get("status") or "investigating").strip()
    components = form.getlist("components") if hasattr(form, "getlist") else form.get("components")
    if isinstance(components, str):
        components = [components] if components else []
    components = [c for c in (components or []) if c in COMPONENT_KEYS]

    if not title:
        raise HTTPException(status_code=400, detail="title required")
    if severity not in SEVERITIES:
        raise HTTPException(status_code=400, detail=f"invalid severity: {severity}")
    if status not in INCIDENT_STATES:
        raise HTTPException(status_code=400, detail=f"invalid status: {status}")

    inc_id = status_db.create_incident(
        title=title,
        description=description,
        severity=severity,
        affected_components=components,
        status=status,
        auto_created=False,
    )
    incident = status_db.get_incident(inc_id)
    log.info("admin %s created incident %d: %r", user.get("email"), inc_id, title)

    # Notify subscribers (non-blocking; failures are logged by the job).
    try:
        await status_subs.notify_incident_event(incident, event_type="created")
    except Exception as exc:
        log.warning("notify subscribers failed for new incident %d: %s", inc_id, exc)

    return RedirectResponse(f"/admin/status#incident-{inc_id}", status_code=303)


@app.post("/admin/incidents/{incident_id}", include_in_schema=False)
async def admin_update_incident(request: Request, incident_id: int):
    user = _require_admin_user(request)
    if not isinstance(user, dict):
        return user
    inc = status_db.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")

    form = await request.form()
    patch: dict = {}
    if (title := (form.get("title") or "").strip()):
        patch["title"] = title
    if "description" in form:
        patch["description"] = form.get("description") or ""
    if (sev := (form.get("severity") or "").strip()):
        if sev not in SEVERITIES:
            raise HTTPException(status_code=400, detail=f"invalid severity: {sev}")
        patch["severity"] = sev
    if "components" in form or (hasattr(form, "getlist") and form.getlist("components")):
        raw = form.getlist("components") if hasattr(form, "getlist") else [form.get("components")]
        comps = [c for c in raw if c in COMPONENT_KEYS]
        patch["affected_components"] = comps

    status_db.update_incident(incident_id, **patch)
    log.info("admin %s patched incident %d: %s", user.get("email"), incident_id, list(patch.keys()))
    return RedirectResponse(f"/admin/status#incident-{incident_id}", status_code=303)


@app.post("/admin/incidents/{incident_id}/updates", include_in_schema=False)
async def admin_post_incident_update(request: Request, incident_id: int):
    user = _require_admin_user(request)
    if not isinstance(user, dict):
        return user
    inc = status_db.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")

    form = await request.form()
    status = (form.get("status") or "").strip() or inc["status"]
    message = (form.get("message") or "").strip()

    if status not in INCIDENT_STATES:
        raise HTTPException(status_code=400, detail=f"invalid status: {status}")
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    update_id = status_db.add_incident_update(
        incident_id, status=status, message=message,
    )
    if status == "resolved":
        status_db.update_incident(incident_id, resolved_at=int(time.time()))

    fresh_inc = status_db.get_incident(incident_id)
    fresh_update = next(
        (u for u in status_db.list_incident_updates(incident_id) if u["id"] == update_id),
        None,
    )
    log.info("admin %s posted update %d on incident %d: status=%s",
             user.get("email"), update_id, incident_id, status)

    event = "resolved" if status == "resolved" else "updated"
    try:
        await status_subs.notify_incident_event(
            fresh_inc, event_type=event, update=fresh_update,
        )
    except Exception as exc:
        log.warning("notify subscribers failed for incident %d update: %s", incident_id, exc)

    return RedirectResponse(f"/admin/status#incident-{incident_id}", status_code=303)


@app.post("/admin/incidents/{incident_id}/resolve", include_in_schema=False)
async def admin_resolve_incident(request: Request, incident_id: int):
    user = _require_admin_user(request)
    if not isinstance(user, dict):
        return user
    inc = status_db.get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    if inc["resolved_at"]:
        return RedirectResponse(f"/admin/status#incident-{incident_id}", status_code=303)

    ok = status_db.mark_incident_resolved(incident_id, message="Incident resolved by admin.")
    if ok:
        fresh = status_db.get_incident(incident_id)
        try:
            await status_subs.notify_incident_event(fresh, event_type="resolved")
        except Exception as exc:
            log.warning("notify subscribers failed for resolve %d: %s", incident_id, exc)
        log.info("admin %s resolved incident %d", user.get("email"), incident_id)

    return RedirectResponse(f"/admin/status#incident-{incident_id}", status_code=303)

"""Admin /admin/cost-alerts — Anthropic AI spend monitoring + kill-switch.

Hooks the page referenced by ``admin_cost_alert.html`` (email template) up
to a real, browser-rendered dashboard. Three new endpoints land here:

    GET  /admin/cost-alerts            HTML page (admin shell)
    GET  /admin/api/ai-cost/refresh    JSON for the bar chart + cards
    POST /admin/ai-cost/kill-switch    toggle the global kill-switch

Registered as a side-effect of being imported at the bottom of
``server.py`` — mirrors :mod:`admin_jobs_routes` so the import-order
contract that keeps these routes above the catch-all stays intact.

Auth model
----------
Every handler goes through ``server._require_admin_user``. The kill-
switch toggle additionally enforces super-admin (admin_level >= 2)
because flipping it pauses every uncached Claude call across the
platform — a stricter bar than other admin mutations. Mutating verbs
flow through the global CSRF middleware (no exemption is registered
for these paths).
"""

from __future__ import annotations

import datetime as _dt
import html
import logging
import time
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import server
from admin_shell import render_admin_page
from queries import ai_cost as ai_cost_q
from security.rate_limiter import rate_limit, get_client_ip


log = logging.getLogger("admin_cost_alerts")


def _admin_key(request: Request) -> str:
    user = server.current_user(request)
    if user and user.get("is_admin"):
        return f"admin_cost_alerts:{user['user_id']}"
    return f"admin_cost_alerts:anon:{get_client_ip(request)}"


# ── Formatting helpers ───────────────────────────────────────────────────


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _fmt_usd_micro(amount: float) -> str:
    """Sub-cent precision for per-call averages."""
    if amount < 0.01:
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    try:
        return _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError):
        return "—"


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


# ── Render helpers (server-side initial paint) ────────────────────────────


def _render_kill_switch_card(status: dict, is_super: bool, csrf_token: str) -> str:
    """Server-rendered kill-switch banner with the toggle form."""
    active = bool(status.get("active"))
    reason = status.get("reason") or ""
    triggered_at = _fmt_ts(status.get("triggered_at"))
    triggered_by = status.get("triggered_by") or ""

    state_label = "ACTIVE" if active else "OFF"
    state_class = "cost-alerts__ks--on" if active else "cost-alerts__ks--off"
    headline = (
        "Uncached Claude calls are blocked." if active
        else "Uncached calls dispatching normally."
    )

    meta_parts: list[str] = []
    if active and reason:
        meta_parts.append(f"Reason: {_esc(reason)}")
    if active and triggered_at != "—":
        meta_parts.append(f"Tripped {_esc(triggered_at)}")
    if active and triggered_by:
        meta_parts.append(f"By {_esc(triggered_by)}")
    meta_html = (
        f'<div class="cost-alerts__ks-meta">{" · ".join(meta_parts)}</div>'
        if meta_parts else ""
    )

    if is_super:
        next_state = "false" if active else "true"
        btn_label = "Deactivate kill-switch" if active else "Activate kill-switch"
        # The toggle posts a JSON body so we plumb CSRF via the header on
        # the fetch() — the form's hidden input is a fallback for the
        # progressive-enhancement degraded path.
        toggle_html = (
            '<form id="cost-alerts-ks-form" '
            'method="post" action="/admin/ai-cost/kill-switch" '
            'class="cost-alerts__ks-form">'
            f'<input type="hidden" name="active" value="{next_state}">'
            f'<input type="hidden" name="_csrf" value="{_esc(csrf_token)}">'
            f'<button type="submit" class="cost-alerts__ks-btn">{btn_label}</button>'
            "</form>"
        )
    else:
        toggle_html = (
            '<span class="cost-alerts__ks-note">Super-admin required to toggle.</span>'
        )

    return (
        f'<section class="cost-alerts__ks {state_class}" '
        'aria-labelledby="cost-alerts-ks-title">'
        '<header class="cost-alerts__ks-head">'
        f'<h3 id="cost-alerts-ks-title" class="cost-alerts__ks-title">Kill-switch</h3>'
        f'<span class="cost-alerts__ks-pill">{state_label}</span>'
        '</header>'
        f'<p class="cost-alerts__ks-headline">{_esc(headline)}</p>'
        f"{meta_html}"
        f"{toggle_html}"
        "</section>"
    )


def _render_bar_chart(daily: list[dict]) -> str:
    """Monochrome SVG bar chart of last-30d daily cost.

    Hover shows the date + cost via a native ``<title>`` element so the
    chart works without JS. The SVG is sized via viewBox so it scales
    cleanly inside the responsive card.
    """
    if not daily:
        return '<div class="cost-alerts__chart-empty">No spend recorded yet.</div>'

    max_cost = max((d.get("cost_usd") or 0) for d in daily) or 1.0
    bar_count = len(daily)
    chart_w = 100.0  # viewBox units, 0..100 horizontally
    chart_h = 36.0   # 0..36 vertically (matches CSS aspect ratio)
    gap = 0.3
    bar_w = max(0.5, (chart_w - gap * (bar_count - 1)) / bar_count)

    parts: list[str] = []
    parts.append(
        f'<svg class="cost-alerts__chart" viewBox="0 0 {chart_w:.2f} {chart_h:.2f}" '
        'preserveAspectRatio="none" role="img" '
        'aria-label="Daily AI spend, last 30 days">'
    )
    for i, d in enumerate(daily):
        cost = float(d.get("cost_usd") or 0)
        # Pixel-perfect minimum bar so zero-spend days still register
        # without blowing out the proportional scale.
        h = (cost / max_cost) * chart_h if max_cost else 0
        if cost > 0:
            h = max(h, 0.4)
        x = i * (bar_w + gap)
        y = chart_h - h
        title = f"{d['day']} — {_fmt_usd(cost)}"
        parts.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h:.2f}" '
            f'class="cost-alerts__chart-bar" data-day="{_esc(d["day"])}" '
            f'data-cost="{cost:.4f}">'
            f'<title>{_esc(title)}</title></rect>'
        )
    parts.append("</svg>")

    # Axis: first / last day labels under the chart.
    first_day = _esc(daily[0]["day"])
    last_day = _esc(daily[-1]["day"])
    parts.append(
        '<div class="cost-alerts__chart-axis">'
        f'<span>{first_day}</span><span>{last_day}</span>'
        '</div>'
    )
    return "".join(parts)


def _render_alerts_table(alerts: list[dict]) -> str:
    if not alerts:
        return (
            '<tr><td colspan="4" class="cost-alerts__empty">'
            "No cost alerts logged. Spend has stayed under threshold."
            "</td></tr>"
        )
    parts: list[str] = []
    for a in alerts:
        parts.append(
            "<tr>"
            f'<td class="cost-alerts__cell-mono">{_esc(_fmt_ts(a.get("sent_at")))}</td>'
            f'<td class="cost-alerts__cell-mono">{_esc(a.get("alert_date"))}</td>'
            f'<td class="cost-alerts__cell-num">{_esc(_fmt_usd(a.get("threshold_usd") or 0))}</td>'
            f'<td class="cost-alerts__cell-num cost-alerts__cell-strong">{_esc(_fmt_usd(a.get("total_cost_usd") or 0))}</td>'
            "</tr>"
        )
    return "".join(parts)


def _render_feature_table(features: list[dict]) -> str:
    if not features:
        return (
            '<tr><td colspan="4" class="cost-alerts__empty">'
            "No Claude calls in the last 24 hours."
            "</td></tr>"
        )
    parts: list[str] = []
    for f in features:
        parts.append(
            "<tr>"
            f'<td>{_esc(f.get("feature"))}</td>'
            f'<td class="cost-alerts__cell-num">{int(f.get("calls") or 0)}</td>'
            f'<td class="cost-alerts__cell-num">{_esc(_fmt_usd(f.get("cost_usd") or 0))}</td>'
            f'<td class="cost-alerts__cell-num cost-alerts__cell-muted">{_esc(_fmt_usd_micro(f.get("avg_cost_per_call") or 0))}</td>'
            "</tr>"
        )
    return "".join(parts)


# ── JSON: live snapshot for chart refresh ─────────────────────────────────


@server.app.get("/admin/api/ai-cost/refresh")
@rate_limit(limit=300, window_seconds=60, key_func=_admin_key)
async def admin_api_ai_cost_refresh(request: Request) -> JSONResponse:
    """Return the chart + feature + alert payload in one round trip."""
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")

    mtd = ai_cost_q.get_total_cost_mtd()
    today = ai_cost_q.get_total_cost(window_hours=24)
    daily = ai_cost_q.get_daily_costs(days=30)
    features = ai_cost_q.get_per_feature_costs(window_hours=24)
    alerts = ai_cost_q.list_cost_alerts(limit=50)
    kill_switch = ai_cost_q.get_kill_switch_status()

    return JSONResponse({
        "mtd_usd": mtd,
        "trailing_24h_usd": today,
        "daily": daily,
        "features": features,
        "alerts": alerts,
        "kill_switch": kill_switch,
        "generated_at": int(time.time()),
    })


# ── POST: kill-switch toggle ──────────────────────────────────────────────


@server.app.post("/admin/ai-cost/kill-switch")
@rate_limit(limit=20, window_seconds=60, key_func=_admin_key)
async def admin_ai_cost_kill_switch(request: Request):
    """Toggle the global Claude kill-switch.

    Body accepts both JSON (``{"active": bool, "reason": str}``) and
    form-urlencoded (``active=true|false&reason=...``) so the page's
    hidden-form fallback works without JS.

    Super-admin only (admin_level >= 2). CSRF is enforced by the global
    middleware — JSON callers send ``x-csrf-token`` header; form callers
    submit the ``_csrf`` hidden input.
    """
    user = server._require_admin_user(request)
    if not isinstance(user, dict):  # pragma: no cover — defensive
        raise HTTPException(status_code=403, detail="Admin required")
    if int(user.get("admin_level") or 1) < 2:
        raise HTTPException(status_code=403, detail="Super-admin required")

    # Accept JSON or form. JSON requests use the x-csrf-token header;
    # form requests carry the CSRF token in the _csrf hidden input —
    # the middleware validates both before we ever see the body.
    content_type = request.headers.get("content-type", "").lower()
    active = False
    reason: Optional[str] = None
    redirect_after = False

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        active = bool(body.get("active"))
        reason = (body.get("reason") or "").strip() or None
    else:
        form = await request.form()
        active_raw = (form.get("active") or "").strip().lower()
        active = active_raw in ("1", "true", "yes", "on")
        reason = (form.get("reason") or "").strip() or None
        redirect_after = True

    ai_cost_q.set_kill_switch(
        active=active,
        reason=reason,
        triggered_by=user.get("email") or f"admin_{user.get('user_id')}",
    )
    log.info(
        "Kill-switch %s by %s (reason=%s)",
        "activated" if active else "deactivated",
        user.get("email"), reason,
    )

    status = ai_cost_q.get_kill_switch_status()
    if redirect_after:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/cost-alerts", status_code=303)
    return JSONResponse({"ok": True, **status})


# ── HTML page ─────────────────────────────────────────────────────────────


@server.app.get("/admin/cost-alerts", response_class=HTMLResponse)
async def admin_cost_alerts_page(request: Request):
    """Render the /admin/cost-alerts dashboard inside the admin shell."""
    user = server._require_admin_user(request, page=True)
    if user is None:
        return server._denied_response(request)
    if not isinstance(user, dict):
        return user  # RedirectResponse for 2FA

    is_super = int(user.get("admin_level") or 1) >= 2

    try:
        mtd = ai_cost_q.get_total_cost_mtd()
        trailing_24h = ai_cost_q.get_total_cost(window_hours=24)
        daily = ai_cost_q.get_daily_costs(days=30)
        features = ai_cost_q.get_per_feature_costs(window_hours=24)
        alerts = ai_cost_q.list_cost_alerts(limit=50)
        kill_switch = ai_cost_q.get_kill_switch_status()
    except Exception:
        log.exception("admin_cost_alerts_page: initial snapshot failed")
        mtd = 0.0
        trailing_24h = 0.0
        daily, features, alerts = [], [], []
        kill_switch = {"active": False, "reason": None,
                       "triggered_at": None, "triggered_by": None}

    total_calls_24h = sum(int(f.get("calls") or 0) for f in features)
    feature_count = len(features)
    alert_count = len(alerts)

    # CSRF token for the kill-switch form. Falls back to a freshly
    # generated token if the cookie isn't set yet; the middleware will
    # ensure it lands on the response so the next POST succeeds.
    csrf_token = (
        request.cookies.get(server.CSRF_COOKIE_NAME)
        or getattr(getattr(request, "state", None), "csrf_token", None)
        or server._generate_csrf_token()
    )

    return render_admin_page(
        request,
        "admin/cost_alerts.html",
        page_title="AI Cost Alerts",
        active_route="cost-alerts",
        breadcrumb=[("Admin", "/admin"), ("AI Cost Alerts", "/admin/cost-alerts")],
        raw_mtd_total=_fmt_usd(mtd),
        raw_trailing_24h=_fmt_usd(trailing_24h),
        raw_total_calls_24h=str(total_calls_24h),
        raw_feature_count=str(feature_count),
        raw_alert_count=str(alert_count),
        raw_kill_switch=_render_kill_switch_card(kill_switch, is_super, csrf_token),
        raw_chart=_render_bar_chart(daily),
        raw_feature_rows=_render_feature_table(features),
        raw_alert_rows=_render_alerts_table(alerts),
    )

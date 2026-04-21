"""
Billing UI routes for narve.ai — /settings/billing and /api/v1/billing/*.

Loaded at the end of server.py (same pattern as status_routes.py). Routes
register themselves as a side effect of import via `@app.get(...)` decorators,
so a single `import billing_routes` is enough to wire them up.

Endpoints:

    GET  /settings/billing              -> full in-app billing page
    POST /settings/billing/cancel       -> flip all active subs to 'cancelled'
    POST /settings/billing/resubscribe  -> reactivate still-in-window subs
    POST /settings/billing/addon        -> add an add-on (only 'trading' wired)
    POST /settings/billing/addon/cancel -> remove an add-on
    GET  /api/v1/billing/invoices       -> Stripe-shaped JSON invoice list
    GET  /api/v1/billing/invoices/{id}/pdf -> 501 stub until Stripe is wired
    POST /api/v1/billing/portal         -> Stripe Customer Portal (stub,
                                           redirects to /enquire)

Stripe integration remains stubbed (see backend/payments/stripe_stub.py). All
mutation routes operate on the local subscriptions table so the UI is fully
exercisable against the in-process test DB.

Proration is computed client-side in static/settings_billing.js — this module
only surfaces the data needed to drive that calculator.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import server
from server import (
    app,
    render_page,
    current_user,
    get_subdomain,
    proxy_request,
    _user_plan_info,
    _role_badge,
    PLAN_DEFS,
    TRADING_ADDON,
)

log = logging.getLogger("billing")


# ── Plan catalog ─────────────────────────────────────────────────────────────
#
# Pulled from the server's PLAN_DEFS / TRADING_ADDON at import time so
# pricing stays in sync across the codebase. USD-only; the existing /billing
# page handles GBP/USD dual display.
PLAN_CATALOG_USD: Dict[str, Dict[str, Any]] = {
    "trader": {
        "key": "trader",
        "label": "Trader",
        "monthly_usd": PLAN_DEFS["trader"]["monthly_usd"],
        "annual_usd": PLAN_DEFS["trader"]["annual_usd"],
        "desc": "For individual traders who want 3 dashboards and core credibility scores.",
        "features": [
            "3 dashboard credits",
            "30-day data window",
            "Basic credibility scores",
            "Standard support",
        ],
    },
    "pro": {
        "key": "pro",
        "label": "Pro",
        "monthly_usd": PLAN_DEFS["pro"]["monthly_usd"],
        "annual_usd": PLAN_DEFS["pro"]["annual_usd"],
        "desc": "For professionals who need every dashboard and Signal Search.",
        "features": [
            "Unlimited dashboards",
            "6-month data window",
            "Per-category credibility",
            "Signal Search",
            "Push notifications",
        ],
    },
    "enterprise": {
        "key": "enterprise",
        "label": "Enterprise",
        "monthly_usd": 0,
        "annual_usd": 0,
        "desc": "For teams and funds. Custom SLA, API access, intelligence add-on.",
        "features": [
            "Everything in Pro",
            "Intelligence Add-on",
            "Dedicated Slack channel",
            "Custom SLA",
            "API access",
        ],
    },
}


def _fmt_date(ts: int) -> str:
    """Locale-friendly date formatter. Handles %-d unavailability on Windows."""
    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    try:
        return dt.strftime("%B %-d, %Y")
    except ValueError:
        return dt.strftime("%B %d, %Y")


def _current_plan_amount_usd(plan: Optional[str], interval: Optional[str]) -> int:
    """Return the USD amount for the given plan+interval, or 0 if unknown."""
    if not plan or plan not in PLAN_DEFS:
        return 0
    pd = PLAN_DEFS[plan]
    return int(pd["annual_usd"]) if interval == "annual" else int(pd["monthly_usd"])


def _hydrate_cancelled_plan(pinfo: dict, subs: dict) -> dict:
    """_user_plan_info only surfaces *active* plans. When the user has a
    cancelled ``__plan__`` sub that's still in-window, copy its fields into
    pinfo so the UI can show "Pro — Cancelled (14 days left)" instead of
    flashing a "No plan" empty state."""
    if pinfo.get("plan"):
        return pinfo
    plan_sub = subs.get("__plan__")
    if not plan_sub or plan_sub["status"] != "cancelled":
        return pinfo
    raw = (plan_sub["plan"] or "")
    if raw.startswith("trader"):
        pinfo["plan"] = "trader"
    elif raw.startswith("pro"):
        pinfo["plan"] = "pro"
    else:
        return pinfo
    if "_annual" in raw:
        pinfo["interval"] = "annual"
    elif "_monthly" in raw:
        pinfo["interval"] = "monthly"
    pinfo["expires_at"] = plan_sub["expires_at"]
    return pinfo


def _plan_status(pinfo: dict, subs: dict) -> str:
    """Return one of ``active`` / ``cancelled`` / ``downgrading`` / ``none``.

    ``cancelled`` = user explicitly cancelled but still has access through
    expires_at (pending cancellation). ``none`` = no plan at all.
    """
    if pinfo.get("is_admin") and not pinfo.get("plan"):
        return "active"
    plan_sub = subs.get("__plan__")
    if plan_sub and plan_sub["status"] == "cancelled":
        return "cancelled"
    if not pinfo.get("plan"):
        return "none"
    if pinfo.get("downgrading"):
        return "downgrading"
    non_plan_subs = [s for s in subs.values() if s and s["dashboard_key"] != "__plan__"]
    if non_plan_subs and all(s["status"] == "cancelled" for s in non_plan_subs):
        return "cancelled"
    return "active"


def _render_current_plan(pinfo: dict, subs: dict, status: str) -> str:
    """Current plan block — price, renewal date, features, add-ons, actions."""
    plan = pinfo.get("plan")
    interval = pinfo.get("interval") or "monthly"
    is_admin = pinfo.get("is_admin")

    if not plan and not is_admin:
        return (
            '<div class="sb-current-plan">'
            '<div>'
            '<span class="sb-plan-badge">No plan</span>'
            '<div class="sb-plan-name">No active subscription</div>'
            '<div class="sb-plan-price">Pick a plan below to get started.</div>'
            '</div>'
            '<div class="sb-plan-actions">'
            '<a href="#change-plan" class="sb-btn sb-btn-primary">Choose a plan</a>'
            '</div>'
            '</div>'
        )

    if is_admin and not plan:
        return (
            '<div class="sb-current-plan">'
            '<div>'
            '<span class="sb-plan-badge">Admin</span>'
            '<div class="sb-plan-name">Admin Access</div>'
            '<div class="sb-plan-price">Full access to every dashboard via admin privileges.</div>'
            '</div>'
            '</div>'
        )

    pdef = PLAN_DEFS.get(plan, PLAN_DEFS["trader"])
    label = pdef["label"]
    amount = pdef["annual_usd"] if interval == "annual" else pdef["monthly_usd"]
    period_word = "year" if interval == "annual" else "month"

    badge_html = '<span class="sb-plan-badge">Active</span>'
    renewal_line = ""
    if pinfo.get("expires_at"):
        renew_str = _fmt_date(pinfo["expires_at"])
        days_left = max(0, int((pinfo["expires_at"] - time.time()) // 86400))
        if status == "cancelled":
            badge_html = '<span class="sb-plan-badge sb-plan-badge-red">Cancelled</span>'
            renewal_line = (
                f'<div class="sb-plan-price">Access ends <strong>{html.escape(renew_str)}</strong> ({days_left} days)</div>'
            )
        elif status == "downgrading":
            badge_html = '<span class="sb-plan-badge sb-plan-badge-amber">Downgrading</span>'
            renewal_line = (
                f'<div class="sb-plan-price">Downgrades to Trader <strong>{html.escape(renew_str)}</strong> ({days_left} days)</div>'
            )
        else:
            renewal_line = (
                f'<div class="sb-plan-price">${amount:,}/{period_word} &middot; renews <strong>{html.escape(renew_str)}</strong> ({days_left} days)</div>'
            )
    else:
        renewal_line = f'<div class="sb-plan-price">${amount:,}/{period_word}</div>'

    feats_parts = []
    for feat in PLAN_CATALOG_USD.get(plan, {}).get("features", []):
        feats_parts.append(
            '<div class="sb-plan-feature yes"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3.5 8 7 11.5 12.5 5"/></svg>'
            f'<span>{html.escape(feat)}</span></div>'
        )

    addon_rows = []
    uid = pinfo.get("_user_id")
    trading_status = db.get_trading_addon_status(uid) if uid else {"active": False}
    if trading_status.get("active"):
        addon_rows.append(
            '<div class="sb-plan-feature yes"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3.5 8 7 11.5 12.5 5"/></svg>'
            '<span>Trading Add-on ($29/month)</span></div>'
        )
    else:
        addon_rows.append(
            '<div class="sb-plan-feature no"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>'
            '<span>Trading Add-on</span></div>'
        )
    addon_rows.append(
        '<div class="sb-plan-feature no"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="4" x2="12" y2="12"/><line x1="12" y1="4" x2="4" y2="12"/></svg>'
        '<span>Intelligence Add-on</span></div>'
    )

    action_btns = [
        '<form method="post" action="/api/v1/billing/portal" style="display:inline">'
        '<button type="submit" class="sb-btn sb-btn-outline">Manage subscription →</button>'
        '</form>'
    ]
    if status == "active":
        # Link directly to the multi-step retention flow — the in-page
        # modal is kept around for degraded-JS fallback but the primary
        # path is the dedicated /settings/billing/cancel-flow page.
        action_btns.append(
            '<a href="/settings/billing/cancel-flow" class="sb-btn sb-btn-ghost">Cancel subscription</a>'
        )
    elif status == "cancelled":
        action_btns.append(
            '<form method="post" action="/settings/billing/resubscribe" style="display:inline">'
            '<button type="submit" class="sb-btn sb-btn-primary">Resubscribe</button>'
            '</form>'
        )

    interval_suffix = f' ({interval.title()})' if interval else ''
    return (
        '<div class="sb-current-plan">'
        '<div>'
        f'{badge_html}'
        f'<div class="sb-plan-name">{html.escape(label)}{interval_suffix}</div>'
        f'{renewal_line}'
        '<div class="sb-plan-features">'
        f'{"".join(feats_parts)}'
        '<div style="grid-column: 1 / -1; margin-top:10px; font-size:11px; font-weight:700; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.04em">Add-ons</div>'
        f'{"".join(addon_rows)}'
        '</div>'
        '</div>'
        '<div class="sb-plan-actions">'
        f'{"".join(action_btns)}'
        '</div>'
        '</div>'
    )


def _render_plan_cards(current_plan_key: Optional[str], interval: str) -> str:
    """Render the three plan cards for the Change plan section."""
    cards = []
    for key in ("trader", "pro", "enterprise"):
        c = PLAN_CATALOG_USD[key]
        amount = c.get("annual_usd") if interval == "annual" else c.get("monthly_usd")
        period = "/yr" if interval == "annual" else "/mo"
        is_current = key == current_plan_key
        current_badge = '<span class="sb-card-current">Current</span>' if is_current else ""
        feats = "".join(f'<li>{html.escape(f)}</li>' for f in c["features"])
        if key == "enterprise":
            price_html = '<div class="sb-card-price">Custom<span class="sb-card-price-period"></span></div>'
            cta_html = '<a href="/enquire" class="sb-btn sb-btn-outline sb-card-cta">Contact sales</a>'
        else:
            price_html = (
                f'<div class="sb-card-price" data-price>${amount:,}'
                f'<span class="sb-card-price-period" data-period>{period}</span></div>'
            )
            cta_cls = "sb-btn-primary" if not is_current else "sb-btn-outline"
            cta_label = "Current plan" if is_current else (
                "Upgrade" if key == "pro" else ("Downgrade" if current_plan_key == "pro" else "Subscribe")
            )
            disabled_attr = ' disabled' if is_current else ''
            cta_html = (
                f'<button type="button" class="sb-btn {cta_cls} sb-card-cta" '
                f'data-change-plan="{html.escape(key)}" data-interval="{html.escape(interval)}"'
                f'{disabled_attr}>{html.escape(cta_label)}</button>'
            )
        cls = "sb-card current" if is_current else "sb-card"
        cards.append(
            f'<div class="{cls}" data-plan-card="{html.escape(key)}" data-current-interval="{html.escape(interval)}">'
            f'<div class="sb-card-label"><span class="sb-card-name">{html.escape(c["label"])}</span>{current_badge}</div>'
            f'{price_html}'
            f'<div class="sb-card-desc">{html.escape(c["desc"])}</div>'
            f'<ul class="sb-card-feats">{feats}</ul>'
            f'{cta_html}'
            f'<input type="hidden" data-interval-input value="{html.escape(interval)}">'
            '</div>'
        )
    return "".join(cards)


def _render_addons(user_id: int) -> str:
    """Render the Add-ons section."""
    trading = db.get_trading_addon_status(user_id)
    if trading.get("active"):
        trading_cta = (
            '<form method="post" action="/settings/billing/addon/cancel" style="display:inline">'
            '<input type="hidden" name="addon" value="trading">'
            '<button type="submit" class="sb-btn sb-btn-danger sb-btn-sm">Remove</button>'
            '</form>'
        )
    else:
        trading_cta = (
            '<form method="post" action="/settings/billing/addon" style="display:inline">'
            '<input type="hidden" name="addon" value="trading">'
            '<button type="submit" class="sb-btn sb-btn-primary sb-btn-sm">Add to plan</button>'
            '</form>'
        )
    status_label = (
        '<span class="sb-addon-status">Active</span>' if trading.get("active") else ""
    )

    return (
        '<div class="sb-addon">'
        f'<div><div class="sb-addon-title">Trading Add-on{status_label}</div>'
        '<div class="sb-addon-desc">Unified Polymarket + Kalshi trading from any dashboard.</div>'
        f'<div class="sb-addon-price">${TRADING_ADDON["monthly_usd"]}/month or ${TRADING_ADDON["annual_usd"]}/year</div></div>'
        f'<div>{trading_cta}</div>'
        '</div>'
        '<div class="sb-addon">'
        '<div><div class="sb-addon-title">Intelligence Add-on</div>'
        '<div class="sb-addon-desc">Claude AI assistant across every dashboard. Contact sales for pricing.</div>'
        '<div class="sb-addon-price">$TBD</div></div>'
        '<div><a href="/enquire" class="sb-btn sb-btn-outline sb-btn-sm">Contact sales</a></div>'
        '</div>'
    )


def _render_cancel_losses(pinfo: dict, user_id: int) -> str:
    """'What you lose if you cancel' retention copy."""
    parts = []
    active_count = pinfo.get("active_count") or 0
    if active_count:
        parts.append(f"{active_count} active dashboard subscription{'s' if active_count != 1 else ''}")
    plan = pinfo.get("plan")
    if plan == "pro":
        parts.append("Full credibility engine with per-category breakdowns")
        parts.append("Signal Search across every prediction market")
        parts.append("Push notifications on high-EV signals")
    elif plan == "trader":
        parts.append("3 dashboard credits")
        parts.append("Core credibility scores")
    if db.get_trading_addon_status(user_id).get("active"):
        parts.append("Unified Polymarket + Kalshi trading")
    if not parts:
        parts.append("Everything in your current plan")
    return "".join(f"<li>{html.escape(p)}</li>" for p in parts)


def _derive_invoices(user_id: int, cursor: int = 0, limit: int = 10) -> dict:
    """Stripe-shaped invoice list derived from subscriptions.

    No real invoices exist yet (Stripe is stubbed). Replace with Stripe's
    Invoice list API call once payments are wired.
    """
    subs = db.list_subscriptions(user_id)
    invoices = []
    for s in subs:
        plan_raw = s["plan"] or ""
        started_at = s["started_at"] or 0
        # Only surface plan-level (or standalone) rows — dashboard-specific
        # rows are noisy and don't correspond to discrete invoice events.
        if s["dashboard_key"] != "__plan__" and not plan_raw.startswith("standalone"):
            continue
        interval = "annual" if plan_raw.endswith("_annual") else "monthly"
        base_plan = "trader" if plan_raw.startswith("trader") else (
            "pro" if plan_raw.startswith("pro") else None
        )
        if base_plan:
            amount = PLAN_DEFS[base_plan]["annual_usd"] if interval == "annual" else PLAN_DEFS[base_plan]["monthly_usd"]
            label = f"{PLAN_DEFS[base_plan]['label']} {interval.title()} subscription"
        else:
            amount = 0
            label = plan_raw or "Subscription"
        status = "paid" if s["status"] == "active" else s["status"]
        invoices.append({
            "id": f"sub_{s['id']}",
            "date": started_at,
            "description": label,
            "amount": amount,
            "status": status,
            "pdf_url": None,
        })

    trading = db.get_trading_addon_status(user_id)
    if trading.get("active"):
        invoices.append({
            "id": "addon_trading",
            "date": trading.get("period_end") or int(time.time()),
            "description": "Trading Add-on",
            "amount": TRADING_ADDON["monthly_usd"],
            "status": "paid",
            "pdf_url": None,
        })

    invoices.sort(key=lambda i: i["date"] or 0, reverse=True)
    page = invoices[cursor:cursor + limit]
    next_cursor = cursor + limit if cursor + limit < len(invoices) else None
    return {"invoices": page, "next_cursor": next_cursor, "total": len(invoices)}


# ── Routes (registered on import) ───────────────────────────────────────────


@app.get("/settings/billing", response_class=HTMLResponse, include_in_schema=False)
async def settings_billing_page(request: Request):
    sub = get_subdomain(request)
    if sub:
        return await proxy_request(request, "/settings/billing")
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    subs_dict = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    now_ts = int(time.time())
    pinfo = _user_plan_info(user, subs_dict, now_ts)
    pinfo["_user_id"] = user["user_id"]
    _hydrate_cancelled_plan(pinfo, subs_dict)
    status = _plan_status(pinfo, subs_dict)

    current_plan_block = _render_current_plan(pinfo, subs_dict, status)
    current_interval = pinfo.get("interval") or "monthly"
    plan_cards_html = _render_plan_cards(pinfo.get("plan"), current_interval)
    addons_html = _render_addons(user["user_id"])
    cancel_losses_html = _render_cancel_losses(pinfo, user["user_id"])

    resubscribe_banner = ""
    if status == "cancelled" and pinfo.get("expires_at"):
        renew_str = _fmt_date(pinfo["expires_at"])
        resubscribe_banner = (
            '<div class="sb-resubscribe">'
            f'<div class="sb-resubscribe-text">Your subscription is <strong>cancelled</strong>. You keep access until {html.escape(renew_str)}. We\'d love to have you back.</div>'
            '<form method="post" action="/settings/billing/resubscribe" style="display:inline">'
            '<button type="submit" class="sb-btn sb-btn-primary">Resubscribe</button>'
            '</form>'
            '</div>'
        )

    flash = ""
    saved = request.query_params.get("saved")
    if saved == "cancelled":
        flash = '<div class="sb-notice sb-notice-success">Your subscription is cancelled. You keep access until the end of the billing period.</div>'
    elif saved == "resubscribed":
        flash = '<div class="sb-notice sb-notice-success">Welcome back! Your subscription is active again.</div>'
    elif saved == "addon_added":
        flash = '<div class="sb-notice sb-notice-success">Trading add-on activated.</div>'
    elif saved == "addon_removed":
        flash = '<div class="sb-notice sb-notice-success">Trading add-on removed.</div>'
    elif saved == "paused":
        days_param = request.query_params.get("days", "30")
        flash = f'<div class="sb-notice sb-notice-success">Subscription paused for {html.escape(days_param)} days. Resume anytime from here.</div>'
    elif saved == "resumed":
        flash = '<div class="sb-notice sb-notice-success">Subscription resumed.</div>'

    # Pause banner — always visible while the pause window is active,
    # independent of the ?saved= flash. Lets the user resume without
    # leaving the billing page.
    pause_banner = ""
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT subscription_paused_until FROM users WHERE id = ?",
                (user["user_id"],),
            ).fetchone()
        if row and row["subscription_paused_until"]:
            pause_banner = (
                '<div class="sb-resubscribe" style="background:rgba(245,158,11,0.08);border-color:rgba(245,158,11,0.3)">'
                '<div class="sb-resubscribe-text">Your subscription is <strong>paused</strong> until '
                f'{html.escape(str(row["subscription_paused_until"]))}. No charges during the pause window.</div>'
                '<form method="post" action="/settings/billing/resume" style="display:inline">'
                '<button type="submit" class="sb-btn sb-btn-primary">Resume now</button>'
                '</form>'
                '</div>'
            )
    except Exception:
        pass
    flash = pause_banner + flash

    danger_zone = ""
    if status == "active" and pinfo.get("plan"):
        danger_zone = (
            '<div class="sb-section" style="border-color:rgba(239,68,68,0.25)">'
            '<div class="sb-section-title">Cancel subscription</div>'
            '<div class="sb-section-desc">You keep access until the end of your current billing period.</div>'
            '<button type="button" class="sb-btn sb-btn-danger" data-open-cancel>Cancel subscription</button>'
            '</div>'
        )

    current_plan_payload = None
    renewal_str = ""
    if pinfo.get("plan"):
        plan_sub_row = subs_dict.get("__plan__")
        started_at_val = None
        if plan_sub_row is not None:
            try:
                started_at_val = plan_sub_row["started_at"]
            except (KeyError, TypeError, IndexError):
                started_at_val = None
        current_plan_payload = {
            "key": pinfo["plan"],
            "label": PLAN_DEFS.get(pinfo["plan"], {}).get("label", pinfo["plan"].title()),
            "interval": pinfo.get("interval") or "monthly",
            "amount_usd": _current_plan_amount_usd(pinfo["plan"], pinfo.get("interval") or "monthly"),
            "started_at": started_at_val,
            "expires_at": pinfo.get("expires_at"),
        }
        if pinfo.get("expires_at"):
            renewal_str = _fmt_date(pinfo["expires_at"])

    trading_status_obj = db.get_trading_addon_status(user["user_id"])
    data_payload = {
        "current_plan": current_plan_payload,
        "catalog": PLAN_CATALOG_USD,
        "addon": {
            "active": bool(trading_status_obj.get("active")),
            "amount_usd": TRADING_ADDON["monthly_usd"],
            "period_end": trading_status_obj.get("period_end"),
        },
        "renewal_str": renewal_str,
        "status": status,
    }
    # Escape </script> so an interpolated string can't close the surrounding
    # <script type="application/json"> element.
    data_json = json.dumps(data_payload).replace("</", "<\\/")

    if pinfo.get("plan"):
        card_display = "Card on file · Visa"
        card_brand_short = "VISA"
        card_expiry = "Stored securely by Stripe"
    else:
        card_display = "No card on file"
        card_brand_short = "—"
        card_expiry = "Managed by Stripe"

    admin_link = '<a href="/admin">Admin</a>' if user.get("is_admin") else ""

    return render_page(
        "settings_billing", request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_current_plan=current_plan_block,
        raw_plan_cards=plan_cards_html,
        raw_addons=addons_html,
        raw_cancel_losses=cancel_losses_html,
        raw_resubscribe_banner=resubscribe_banner,
        raw_flash_banner=flash,
        raw_danger_zone=danger_zone,
        raw_data_json=data_json,
        card_display=card_display,
        card_brand_short=card_brand_short,
        card_expiry=card_expiry,
        monthly_active=("active" if current_interval == "monthly" else ""),
        annual_active=("active" if current_interval == "annual" else ""),
        raw_admin_link=admin_link,
        raw_nav_role=_role_badge(user), _is_admin=user.get("is_admin"),
    )


def _retention_stats(user_id: int) -> dict:
    """Pull the numbers shown on the cancel retention screen.

    Each query is defensive: a missing table (schema skew between branches)
    returns 0 rather than bubbling an exception. The point of this screen
    is to get the user to pause — showing "Loading…" forever or 500-ing
    defeats the whole point.
    """
    stats = {
        "watchlist_count": 0,
        "saved_signals_count": 0,
        "streak_days": 0,
        "accuracy_pct": None,
        "prediction_count": 0,
    }
    try:
        with db.conn() as c:
            # Watchlist: followed_sources rows where notify_on_prediction
            # is set OR any saved prediction is still "active" (unresolved).
            row = c.execute(
                "SELECT COUNT(*) AS n FROM followed_sources WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            stats["watchlist_count"] = int(row["n"] if row else 0)
            row = c.execute(
                "SELECT COUNT(*) AS n FROM saved_predictions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            stats["saved_signals_count"] = int(row["n"] if row else 0)
            # Predictions made by the user — read from user_prediction_stats
            # if available (populated by the scoring pipeline), else count
            # raw predictions rows keyed to the user.
            try:
                row = c.execute(
                    "SELECT total_predictions, accuracy_pct, current_streak_days "
                    "FROM user_prediction_stats WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if row:
                    stats["prediction_count"] = int(row["total_predictions"] or 0)
                    if row["accuracy_pct"] is not None:
                        stats["accuracy_pct"] = float(row["accuracy_pct"])
                    stats["streak_days"] = int(row["current_streak_days"] or 0)
            except Exception:
                pass
            # Fallback streak: count distinct days with a 'login' event in
            # engagement_events, looking back up to 60 days.
            if stats["streak_days"] == 0:
                try:
                    row = c.execute(
                        """
                        SELECT COUNT(DISTINCT date(created_at)) AS d
                        FROM engagement_events
                        WHERE user_id = ?
                          AND event_type = 'login'
                          AND created_at >= datetime('now', '-60 days')
                        """,
                        (user_id,),
                    ).fetchone()
                    stats["streak_days"] = int(row["d"] if row else 0)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("retention_stats: failed for uid=%s: %s", user_id, exc)
    return stats


def _render_cancel_step1(user_id: int, pinfo: dict) -> str:
    """Step 1 of the 3-step cancel flow: show what the user loses."""
    stats = _retention_stats(user_id)
    plan_label = PLAN_DEFS.get(pinfo.get("plan") or "trader", {}).get("label", "Pro")
    bullet = lambda txt: f'<li style="padding:6px 0;border-bottom:1px solid var(--border)">{html.escape(txt)}</li>'
    bullets = []
    if stats["watchlist_count"]:
        bullets.append(bullet(f"{stats['watchlist_count']} sources you're following"))
    if stats["saved_signals_count"]:
        bullets.append(bullet(f"{stats['saved_signals_count']} saved signals"))
    if stats["streak_days"] and stats["streak_days"] >= 3:
        bullets.append(bullet(f"Your {stats['streak_days']}-day research streak"))
    if stats["accuracy_pct"] is not None and stats["prediction_count"]:
        acc = round(stats["accuracy_pct"] * 100 if stats["accuracy_pct"] <= 1.0 else stats["accuracy_pct"], 1)
        bullets.append(bullet(f"{acc}% accuracy across your {stats['prediction_count']} predictions"))
    if not bullets:
        bullets.append(bullet(f"Every {plan_label} feature you've come to rely on"))

    return (
        '<h2 style="margin:0 0 12px">Before you cancel</h2>'
        f'<p style="margin:0 0 16px;color:var(--text-secondary)">Here\'s what you\'d lose if you cancel your {html.escape(plan_label)} subscription:</p>'
        f'<ul style="list-style:none;padding:0;margin:0 0 24px">{"".join(bullets)}</ul>'
        '<form method="post" action="/settings/billing/cancel" style="display:flex;gap:8px;flex-direction:column">'
        '<input type="hidden" name="step" value="1">'
        '<label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:4px">Reason for leaving (optional)</label>'
        '<select name="reason" style="padding:8px;background:var(--bg-raised);border:1px solid var(--border);border-radius:6px;color:var(--text-primary)">'
        '<option value="">—</option>'
        '<option value="too_expensive">Too expensive</option>'
        '<option value="not_using">Not using it enough</option>'
        '<option value="found_alternative">Found an alternative</option>'
        '<option value="missing_feature">Missing a feature I need</option>'
        '<option value="other">Other</option>'
        '</select>'
        '<div style="display:flex;gap:8px;margin-top:20px">'
        '<a href="/settings/billing" class="sb-btn sb-btn-primary" style="flex:1;text-align:center">Keep my subscription</a>'
        '<button type="submit" class="sb-btn sb-btn-ghost" style="flex:1">Continue to cancel →</button>'
        '</div>'
        '</form>'
    )


def _render_cancel_step2(attempt_id: int) -> str:
    """Step 2: offer a pause before the final cancel confirmation."""
    return (
        '<h2 style="margin:0 0 12px">Rather than cancel, would you like to pause?</h2>'
        '<p style="margin:0 0 16px;color:var(--text-secondary)">'
        'Pausing keeps your data, settings, and watchlist. You can resume anytime '
        'from /settings/billing. No extra charge.'
        '</p>'
        '<div style="display:flex;gap:8px;flex-direction:column">'
        f'<form method="post" action="/settings/billing/pause">'
        '<input type="hidden" name="attempt_id" value="' + str(attempt_id) + '">'
        '<input type="hidden" name="days" value="30">'
        '<button type="submit" class="sb-btn sb-btn-primary" style="width:100%">Pause for 30 days</button>'
        '</form>'
        f'<form method="post" action="/settings/billing/pause">'
        '<input type="hidden" name="attempt_id" value="' + str(attempt_id) + '">'
        '<input type="hidden" name="days" value="60">'
        '<button type="submit" class="sb-btn sb-btn-outline" style="width:100%">Pause for 60 days</button>'
        '</form>'
        f'<form method="post" action="/settings/billing/cancel">'
        '<input type="hidden" name="step" value="3">'
        '<input type="hidden" name="attempt_id" value="' + str(attempt_id) + '">'
        '<button type="submit" class="sb-btn sb-btn-ghost" style="width:100%">No, cancel anyway</button>'
        '</form>'
        '</div>'
    )


def _open_cancel_attempt(user_id: int, reason: str, reached_step: int) -> int:
    """Record a new cancel_attempts row and return its id."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO cancellation_attempts (user_id, reason, reached_step) "
            "VALUES (?, ?, ?)",
            (user_id, (reason or "").strip()[:80] or None, reached_step),
        )
        return int(cur.lastrowid or 0)


def _finalize_cancel_attempt(attempt_id: int, outcome: str, reached_step: int, pause_days: int | None = None) -> None:
    """Close out a cancel_attempts row with the final outcome."""
    if not attempt_id:
        return
    with db.conn() as c:
        c.execute(
            "UPDATE cancellation_attempts "
            "SET outcome = ?, reached_step = MAX(reached_step, ?), "
            "    pause_days = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (outcome, reached_step, pause_days, attempt_id),
        )


def _queue_winback_emails(user_id: int, email: str) -> None:
    """Queue +7d and +30d win-back emails. Payload-only — the SMTP send
    wires up separately. Failures here are logged, not raised: a missed
    email queue shouldn't block the cancel from completing."""
    try:
        import asyncio
        from jobs import enqueue_job
        now_ts = int(time.time())
        payloads = [
            ("winback_7d", now_ts + 7 * 86400),
            ("winback_30d", now_ts + 30 * 86400),
        ]
        coros = [
            enqueue_job(
                "send_email",
                to=email,
                template=tmpl,
                context={"user_id": user_id},
                tags=["winback", tmpl],
                run_at=run_at_ts,
            )
            for tmpl, run_at_ts in payloads
        ]
        # Fire-and-forget — we can await via asyncio.gather in an event
        # loop (we're inside an async handler caller), but the current
        # site's call site may be sync. Use run_until on a transient loop
        # only if we aren't already in one.
        loop = None
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                for coro in coros:
                    asyncio.create_task(coro)
                return
        except RuntimeError:
            loop = None
        # No running loop — synchronously drive the enqueues. This path
        # is rare (tests, stand-alone scripts) and tolerates blocking.
        import asyncio as _asyncio
        _asyncio.run(_asyncio.gather(*coros, return_exceptions=True))
    except Exception as exc:
        log.warning("winback enqueue failed for uid=%s: %s", user_id, exc)


@app.get("/settings/billing/cancel-flow", response_class=HTMLResponse, include_in_schema=False)
async def settings_billing_cancel_flow(request: Request):
    """The 3-step retention UI — step 1 renders by default; step 2 is
    driven by a POST that creates a cancellation_attempt row and then
    redirects back here with ?step=2&attempt_id=...
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
    pinfo = _user_plan_info(user, subs, int(time.time()))
    pinfo["_user_id"] = user["user_id"]

    step = request.query_params.get("step", "1")
    attempt_id_raw = request.query_params.get("attempt_id", "")
    try:
        attempt_id = int(attempt_id_raw)
    except ValueError:
        attempt_id = 0

    if step == "2" and attempt_id:
        inner = _render_cancel_step2(attempt_id)
    else:
        inner = _render_cancel_step1(user["user_id"], pinfo)

    return render_page(
        "settings_billing_cancel",
        request=request,
        email=user["email"],
        username=user.get("username", user["email"]),
        raw_cancel_inner=inner,
        raw_nav_role=_role_badge(user),
        _is_admin=user.get("is_admin"),
        raw_admin_link=('<a href="/admin">Admin</a>' if user.get("is_admin") else ""),
    )


@app.post("/settings/billing/cancel", include_in_schema=False)
async def settings_billing_cancel(
    request: Request,
    reason: str = Form(""),
    step: str = Form("1"),
    attempt_id: str = Form(""),
):
    """Three-step cancel funnel.

    step=1 → record attempt, advance to step 2 (pause offer).
    step=3 → finalize cancel (flip subs to 'cancelled', queue win-back emails).
    Any other value → treat as step 1.
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    try:
        attempt_id_int = int(attempt_id)
    except ValueError:
        attempt_id_int = 0

    if step == "3":
        # Final confirmation — flip subs + queue win-back emails.
        with db.conn() as c:
            c.execute(
                "UPDATE subscriptions SET status = 'cancelled' "
                "WHERE user_id = ? AND status = 'active'",
                (user["user_id"],),
            )
        _finalize_cancel_attempt(attempt_id_int, outcome="cancelled", reached_step=3)
        _queue_winback_emails(user["user_id"], user["email"])
        log.info(
            "User %s cancelled subscription (attempt=%s)",
            user.get("username", user["email"]),
            attempt_id_int,
        )
        return RedirectResponse("/settings/billing?saved=cancelled", status_code=302)

    # Default: step 1 → record attempt, forward to step 2.
    reason_clean = (reason or "").strip()[:80]
    attempt_id_int = _open_cancel_attempt(user["user_id"], reason_clean, reached_step=1)
    return RedirectResponse(
        f"/settings/billing/cancel-flow?step=2&attempt_id={attempt_id_int}",
        status_code=302,
    )


@app.post("/settings/billing/pause", include_in_schema=False)
async def settings_billing_pause(
    request: Request,
    days: str = Form("30"),
    attempt_id: str = Form(""),
):
    """Pause the subscription for N days. Logs a pause row + finalizes
    the cancellation_attempt as outcome='paused'.
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    try:
        pause_days = max(7, min(90, int(days)))
    except ValueError:
        pause_days = 30
    try:
        attempt_id_int = int(attempt_id)
    except ValueError:
        attempt_id_int = 0

    now_ts = int(time.time())
    resume_ts = now_ts + pause_days * 86400
    with db.conn() as c:
        c.execute(
            "INSERT INTO subscription_pauses (user_id, resume_at) "
            "VALUES (?, datetime(?, 'unixepoch'))",
            (user["user_id"], resume_ts),
        )
        c.execute(
            "UPDATE users SET subscription_paused_until = datetime(?, 'unixepoch') "
            "WHERE id = ?",
            (resume_ts, user["user_id"]),
        )
    _finalize_cancel_attempt(attempt_id_int, outcome="paused", reached_step=2, pause_days=pause_days)
    log.info(
        "User %s paused subscription for %d days (attempt=%s)",
        user.get("username", user["email"]), pause_days, attempt_id_int,
    )
    return RedirectResponse(
        f"/settings/billing?saved=paused&days={pause_days}",
        status_code=302,
    )


@app.post("/settings/billing/resume", include_in_schema=False)
async def settings_billing_resume(request: Request):
    """Resume a paused subscription early."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    with db.conn() as c:
        c.execute(
            "UPDATE users SET subscription_paused_until = NULL WHERE id = ?",
            (user["user_id"],),
        )
        c.execute(
            "UPDATE subscription_pauses SET resumed_early_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND resumed_early_at IS NULL",
            (user["user_id"],),
        )
    log.info("User %s resumed subscription early", user.get("username", user["email"]))
    return RedirectResponse("/settings/billing?saved=resumed", status_code=302)


@app.post("/settings/billing/resubscribe", include_in_schema=False)
async def settings_billing_resubscribe(request: Request):
    """Reactivate cancelled subs that haven't yet expired."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            "UPDATE subscriptions SET status = 'active' "
            "WHERE user_id = ? AND status = 'cancelled' "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user["user_id"], now),
        )
    log.info("User %s resubscribed", user.get("username", user["email"]))
    return RedirectResponse("/settings/billing?saved=resubscribed", status_code=302)


@app.post("/settings/billing/addon", include_in_schema=False)
async def settings_billing_addon_add(request: Request, addon: str = Form(...)):
    """Add an add-on. Only 'trading' is wired up — Stripe stubbed."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if addon != "trading":
        return RedirectResponse("/settings/billing", status_code=302)
    now = int(time.time())
    db.set_trading_addon(user["user_id"], True, period_end=now + 30 * 86400)
    log.info("User %s added trading add-on", user.get("username", user["email"]))
    return RedirectResponse("/settings/billing?saved=addon_added", status_code=302)


@app.post("/settings/billing/addon/cancel", include_in_schema=False)
async def settings_billing_addon_cancel(request: Request, addon: str = Form(...)):
    """Remove an add-on."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    if addon != "trading":
        return RedirectResponse("/settings/billing", status_code=302)
    db.set_trading_addon(user["user_id"], False, None)
    log.info("User %s removed trading add-on", user.get("username", user["email"]))
    return RedirectResponse("/settings/billing?saved=addon_removed", status_code=302)


@app.get("/api/v1/billing/invoices", include_in_schema=False)
async def api_billing_invoices(request: Request, cursor: int = 0, limit: int = 10):
    """Paginated invoice list for the logged-in user (cookie auth)."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    limit = max(1, min(int(limit or 10), 50))
    cursor = max(0, int(cursor or 0))
    return JSONResponse(_derive_invoices(user["user_id"], cursor=cursor, limit=limit))


@app.get("/api/v1/billing/invoices/{invoice_id}/pdf", include_in_schema=False)
async def api_billing_invoice_pdf(request: Request, invoice_id: str):
    """Stubbed — returns 501 until Stripe is configured."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return JSONResponse(
        {
            "error": "invoice_pdf_not_available",
            "message": (
                "PDF invoices become available once Stripe is configured. "
                "This is a stub endpoint."
            ),
            "invoice_id": invoice_id,
        },
        status_code=501,
    )


@app.post("/api/v1/billing/portal", include_in_schema=False)
async def api_billing_portal(request: Request):
    """Stripe Customer Portal — stubbed. Redirects to /enquire."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    log.info(
        "User %s requested Stripe portal (stubbed)",
        user.get("username", user["email"]),
    )
    return RedirectResponse("/enquire", status_code=302)

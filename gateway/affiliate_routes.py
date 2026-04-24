"""Route layer for the private affiliate program.

Imported at the end of ``server_features.py`` so ``app`` and the core
server helpers are already defined. Follows the same pattern as the
other feature modules: imports ``app``, ``render_page``, ``current_user``
etc. from ``server`` and registers routes in place.

Three groups of routes:

1. **Public** — ``/partner/{code}`` and the short alias ``/p/{code}``.
   Record click, set 90-day cookie, redirect. No auth.

2. **Affiliate-owner** — ``/settings/affiliate`` page and the
   ``/api/affiliate*`` JSON endpoints. Require the requester to have an
   active ``AffiliateAccount`` (not just any user).

3. **Admin** — ``/admin/affiliates`` list + the create/update/payout
   mutations. Gated by ``_require_admin_user`` identical to the existing
   admin routes in ``server.py``.

The signup-attribution hook ``maybe_attribute_signup(request, user_id)``
is exported for ``/auth/register`` to call inline after ``create_user``
succeeds. Any error inside the hook is swallowed with a warning — a
failing affiliate attribution must never break signup for the user.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import db
import db_affiliate as da
from server import (  # noqa: E402 — late import, matches server_features pattern
    app,
    render_page,
    current_user,
    _require_admin_user,
    _denied_response,
    _get_client_ip,
    _is_rate_limited,
    log,
)


# ── Configuration ─────────────────────────────────────────────────────


_ADMIN_NOTIFY_EMAIL = (
    os.environ.get("AFFILIATE_PAYOUT_ADMIN_EMAIL")
    or os.environ.get("SUPPORT_EMAIL")
    or "support@narve.ai"
)
_COOKIE_IS_SECURE = (os.environ.get("PRODUCTION", "0") == "1")


# ── Cookie helpers ────────────────────────────────────────────────────


def _set_affiliate_cookie(response: Response, code: str) -> None:
    """Drop the 90-day ``affiliate_code`` cookie.

    httpOnly = True because the UI doesn't need JS access; keeps the
    code out of reach of any XSS. sameSite = "lax" so the click from a
    newsletter / podcast show-notes → narve.ai still carries the cookie
    on the landing request but cross-site POSTs don't.
    """
    response.set_cookie(
        key=da.AFFILIATE_COOKIE_NAME,
        value=code,
        max_age=da.AFFILIATE_COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=_COOKIE_IS_SECURE,
        httponly=True,
        samesite="lax",
    )


def _read_affiliate_cookie(request: Request) -> Optional[str]:
    return request.cookies.get(da.AFFILIATE_COOKIE_NAME)


# ── Signup hook (called from /auth/register) ─────────────────────────


def maybe_attribute_signup(request: Request, user_id: int) -> None:
    """Best-effort: if the affiliate_code cookie is set and names a real,
    active affiliate, record the signup against them.

    Every error is swallowed — a failing affiliate attribution must not
    break the signup path. We log so ops can see attribution misses.
    """
    try:
        code = _read_affiliate_cookie(request)
        if not code:
            return
        aff = da.get_affiliate_by_code(code)
        if not aff or not aff["is_active"]:
            log.info("affiliate: cookie %r does not resolve to an active affiliate", code)
            return
        fingerprint = _get_client_ip(request)
        conv_id = da.attach_signup_to_affiliate(
            aff["id"], user_id, fallback_fingerprint=fingerprint,
        )
        if conv_id:
            log.info(
                "affiliate: attributed user_id=%d to affiliate_id=%d conv_id=%d",
                user_id, aff["id"], conv_id,
            )
        else:
            log.info(
                "affiliate: user_id=%d already attributed, skipping", user_id
            )
    except Exception:
        log.exception("affiliate: attribution hook failed for user_id=%d", user_id)


# ── Public affiliate link endpoints ──────────────────────────────────


def _handle_partner_click(request: Request, code: str) -> Response:
    """Shared implementation for ``/partner/{code}`` and ``/p/{code}``.

    Resolves code → affiliate + optional link (via ``?c=<utm>``), bumps
    counters, sets cookie, redirects to /. Silently ignores unknown
    codes (just redirects without cookie) so a tampered URL doesn't
    leak whether a code exists.
    """
    ip = _get_client_ip(request)
    # Light per-IP throttle on the public endpoint — a scraper hammering
    # /partner/XYZ would otherwise inflate click counts for whoever's
    # code they bruteforce-land on. Generous limit; not a DDoS defense.
    if _is_rate_limited(f"affiliate_click:{ip}", limit=30, window=60):
        return RedirectResponse("/", status_code=302)

    affiliate = da.get_affiliate_by_code(code)
    # Redirect silently on unknown / deactivated codes so we don't leak
    # which codes are valid via different response behaviour.
    if not affiliate or not affiliate["is_active"]:
        return RedirectResponse("/", status_code=302)

    # Optional per-campaign link. Ignore unknown utm_campaign silently —
    # we still want the default attribution to stick.
    utm_campaign = request.query_params.get("c") or request.query_params.get("utm_campaign") or ""
    link_id: Optional[int] = None
    if utm_campaign:
        link = da.get_affiliate_link_by_campaign(affiliate["id"], utm_campaign)
        if link:
            link_id = link["id"]

    # Record the click. Any exception here is non-fatal — we still set
    # the cookie + redirect so the user experience is never worse than
    # "affiliate not attributed".
    try:
        da.record_affiliate_click(
            affiliate["id"], link_id=link_id, click_fingerprint=ip,
        )
    except Exception:
        log.exception("affiliate: record_click failed for code=%s", code)

    response = RedirectResponse("/", status_code=302)
    _set_affiliate_cookie(response, affiliate["affiliate_code"])
    return response


@app.get("/partner/{code}")
async def partner_click(request: Request, code: str):
    return _handle_partner_click(request, code)


@app.get("/p/{code}")
async def partner_click_short(request: Request, code: str):
    """Short alias for ``/partner/{code}``. Used in custom tracking links
    (``narve.ai/p/ABC?c=podcast_ep_47``) to keep the URL compact."""
    return _handle_partner_click(request, code)


# ── Affiliate-owner dashboard ────────────────────────────────────────


def _require_active_affiliate(request: Request):
    """Return (user, affiliate_row) tuple or raise HTTPException.

    Used by every ``/api/affiliate*`` endpoint. The ``/settings/affiliate``
    page uses a laxer check so a non-affiliate sees a friendly 404-ish
    page rather than a JSON error.
    """
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    aff = da.get_affiliate_by_user_id(user["user_id"])
    if not aff or not aff["is_active"]:
        raise HTTPException(status_code=403, detail="No active affiliate account")
    return user, aff


def _affiliate_dashboard_context(request: Request, aff, links, convs, summary) -> dict:
    """Pre-render the HTML fragments used by settings_affiliate.html. We
    build HTML server-side rather than client-side because the existing
    template engine uses ``{{ key }}`` string substitution, not Jinja2.
    """
    import html as _html

    apex = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")
    default_link = f"{apex}/partner/{aff['affiliate_code']}"

    link_rows_html: list[str] = []
    for L in links:
        tracking_url = f"{apex}/p/{aff['affiliate_code']}?c={_html.escape(L['utm_campaign'])}"
        label = _html.escape(L["utm_content"] or L["utm_campaign"])
        link_rows_html.append(
            f'<tr>'
            f'<td>{label}</td>'
            f'<td><code>{_html.escape(tracking_url)}</code></td>'
            f'<td>{L["clicks"]}</td>'
            f'<td>{L["conversions"]}</td>'
            f'</tr>'
        )
    if not link_rows_html:
        link_rows_html.append(
            '<tr><td colspan="4" style="opacity:0.6">'
            'No custom tracking links yet.</td></tr>'
        )

    conv_rows_html: list[str] = []
    for c in convs[:20]:
        import datetime as _dt
        date = _dt.datetime.fromtimestamp(
            c["signed_up_at"] or c["clicked_at"]
        ).strftime("%b %d, %Y")
        email_anon = _html.escape(da.anonymise_email(c["referred_email"]))
        commission_pence = c["commission_amount_pence"] or 0
        commission_str = f"£{commission_pence / 100:.2f}" if commission_pence else "pending"
        status = (
            "paid" if c["commission_paid"] else
            "converted" if c["converted_at"] else
            "signed up" if c["signed_up_at"] else
            "clicked"
        )
        conv_rows_html.append(
            f'<tr>'
            f'<td>{date}</td>'
            f'<td>{email_anon}</td>'
            f'<td>{_html.escape(status)}</td>'
            f'<td>{commission_str}</td>'
            f'</tr>'
        )
    if not conv_rows_html:
        conv_rows_html.append(
            '<tr><td colspan="4" style="opacity:0.6">'
            'No conversions yet. Share your link to get started.</td></tr>'
        )

    payout_eligible = summary["pending_pence"] >= da.DEFAULT_PAYOUT_THRESHOLD_PENCE

    return {
        "affiliate_tier": _html.escape(aff["tier"]),
        "affiliate_commission_rate_pct": f"{int(round(aff['commission_rate'] * 100))}",
        "default_link": _html.escape(default_link),
        "total_clicks": str(summary["click_count"]),
        "total_signups": str(summary["conversion_count"]),
        "total_paid_conversions": str(summary["paid_conversion_count"]),
        "total_earned_gbp": f"{summary['earned_pence'] / 100:.2f}",
        "total_paid_gbp": f"{summary['paid_pence'] / 100:.2f}",
        "total_pending_gbp": f"{summary['pending_pence'] / 100:.2f}",
        "payout_threshold_gbp": f"{da.DEFAULT_PAYOUT_THRESHOLD_PENCE / 100:.0f}",
        "payout_eligible": "yes" if payout_eligible else "no",
        "raw_link_rows": "\n".join(link_rows_html),
        "raw_conversion_rows": "\n".join(conv_rows_html),
    }


@app.get("/settings/affiliate", response_class=HTMLResponse)
async def affiliate_dashboard(request: Request):
    """Affiliate performance dashboard. Only visible to users with an
    active ``AffiliateAccount``."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)

    aff = da.get_affiliate_by_user_id(user["user_id"])
    if not aff or not aff["is_active"]:
        # Render a simple info page rather than a 403 — partners who
        # forget they have an account get a useful message.
        return HTMLResponse(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Affiliate dashboard · narve.ai</title></head>"
            "<body style='font-family:Inter,sans-serif;max-width:640px;"
            "margin:60px auto;padding:0 20px;color:#fff;background:#0d0d0d'>"
            "<h1>Affiliate dashboard</h1>"
            "<p style='opacity:0.7'>You don't have an active affiliate "
            "account. Affiliate access is invite-only — if you think "
            "this is a mistake, email "
            f"<a style='color:#fff' href='mailto:{_ADMIN_NOTIFY_EMAIL}'>"
            f"{_ADMIN_NOTIFY_EMAIL}</a>.</p>"
            "<p><a style='color:#fff' href='/dashboards'>← Dashboards</a></p>"
            "</body></html>",
            status_code=200,
        )

    links = da.list_affiliate_links(aff["id"])
    convs = da.list_affiliate_conversions(aff["id"], limit=50)
    summary = da.sum_affiliate_commissions(aff["id"])
    ctx = _affiliate_dashboard_context(request, aff, links, convs, summary)
    return render_page("settings_affiliate", request=request, **ctx)


@app.get("/api/v1/affiliate")
async def api_affiliate_info(request: Request):
    user, aff = _require_active_affiliate(request)
    links = da.list_affiliate_links(aff["id"])
    summary = da.sum_affiliate_commissions(aff["id"])
    apex = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")

    return JSONResponse({
        "affiliate_code": aff["affiliate_code"],
        "tier": aff["tier"],
        "commission_rate": aff["commission_rate"],
        "default_link": f"{apex}/partner/{aff['affiliate_code']}",
        "payout_method": aff["payout_method"],
        "payout_email": aff["payout_email"],
        "summary_pence": summary,
        "summary_gbp": {
            "earned": round(summary["earned_pence"] / 100, 2),
            "paid": round(summary["paid_pence"] / 100, 2),
            "pending": round(summary["pending_pence"] / 100, 2),
        },
        "links": [
            {
                "id": L["id"],
                "utm_campaign": L["utm_campaign"],
                "utm_content": L["utm_content"],
                "clicks": L["clicks"],
                "conversions": L["conversions"],
                "tracking_url": f"{apex}/p/{aff['affiliate_code']}?c={L['utm_campaign']}",
            }
            for L in links
        ],
    })


@app.post("/api/v1/affiliate/links")
async def api_affiliate_create_link(request: Request):
    user, aff = _require_active_affiliate(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    utm_campaign = (body.get("utm_campaign") or "").strip()
    utm_content = body.get("utm_content")
    if utm_content is not None:
        utm_content = str(utm_content).strip()[:200] or None

    if not utm_campaign:
        raise HTTPException(status_code=400, detail="utm_campaign required")

    try:
        link_id = da.create_affiliate_link(aff["id"], utm_campaign, utm_content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    apex = os.environ.get("APP_URL", "https://narve.ai").rstrip("/")
    link = None
    for L in da.list_affiliate_links(aff["id"]):
        if L["id"] == link_id:
            link = L
            break
    if link is None:
        raise HTTPException(status_code=500, detail="link disappeared")
    return JSONResponse({
        "id": link["id"],
        "utm_campaign": link["utm_campaign"],
        "utm_content": link["utm_content"],
        "clicks": link["clicks"],
        "conversions": link["conversions"],
        "tracking_url": f"{apex}/p/{aff['affiliate_code']}?c={link['utm_campaign']}",
    })


@app.get("/api/v1/affiliate/conversions")
async def api_affiliate_conversions(request: Request):
    user, aff = _require_active_affiliate(request)
    convs = da.list_affiliate_conversions(aff["id"], limit=200)
    return JSONResponse({
        "conversions": [
            {
                "id": c["id"],
                # Emails anonymised — affiliates see WHO converted at the
                # handle level (for dispute/fraud reasons) but not the
                # full email. See ``anonymise_email`` for the mask.
                "referred_email": da.anonymise_email(c["referred_email"]),
                "clicked_at": c["clicked_at"],
                "signed_up_at": c["signed_up_at"],
                "converted_at": c["converted_at"],
                "first_payment_pence": c["first_payment_amount_pence"],
                "commission_pence": c["commission_amount_pence"],
                "commission_paid": bool(c["commission_paid"]),
                "commission_paid_at": c["commission_paid_at"],
            }
            for c in convs
        ],
    })


@app.post("/api/v1/affiliate/payout-request")
async def api_affiliate_payout_request(request: Request):
    """Fire off an admin email asking for a manual payout.

    Rate-limited per affiliate account so a user can't spam the admin
    inbox. Idempotent-ish: same request in the window returns 200.
    """
    user, aff = _require_active_affiliate(request)
    summary = da.sum_affiliate_commissions(aff["id"])
    pending = summary["pending_pence"]
    if pending < da.DEFAULT_PAYOUT_THRESHOLD_PENCE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Minimum payout is £{da.DEFAULT_PAYOUT_THRESHOLD_PENCE / 100:.0f}. "
                f"You have £{pending / 100:.2f} pending."
            ),
        )

    # Per-affiliate rate limit (1 request per hour). Keeps the admin
    # inbox clean even if the UI accidentally double-submits.
    if _is_rate_limited(f"affiliate_payout_req:{aff['id']}", limit=1, window=3600):
        return JSONResponse({
            "ok": True,
            "message": "Already requested; admin will process shortly.",
        })

    try:
        # The admin-email send is best-effort — if the email system
        # isn't configured (pilot mode), we still record the request
        # intent via the rate-limit key above.
        _send_admin_payout_notification(aff, summary, user)
    except Exception:
        log.exception("affiliate: payout-request email send failed")

    return JSONResponse({
        "ok": True,
        "message": "Payout requested. You'll hear back within 3 business days.",
    })


def _send_admin_payout_notification(aff, summary: dict, user) -> None:
    """Inline email to the admin inbox describing the payout request.

    We don't use ``email_system.send_template`` because there's no
    dedicated template for admin-facing payout notifications; a plain
    text-ish HTML body is fine for an internal ops ping.
    """
    try:
        from email_system.service import EmailService
    except Exception:
        log.warning("affiliate: EmailService unavailable; skipping admin notif")
        return

    pending_gbp = summary["pending_pence"] / 100
    user_email = user.get("email") or "(unknown)"
    subject = (
        f"[narve.ai] Affiliate payout requested — £{pending_gbp:.2f} for "
        f"{aff['payout_email'] or user_email}"
    )
    body = (
        f"<p>Affiliate id {aff['id']} ({aff['tier']}, {int(aff['commission_rate']*100)}%) "
        f"requested a payout.</p>"
        f"<ul>"
        f"<li>User: {user_email}</li>"
        f"<li>Payout email: {aff['payout_email'] or '(not set)'}</li>"
        f"<li>Payout method: {aff['payout_method'] or '(not set)'}</li>"
        f"<li>Pending: £{pending_gbp:.2f} "
        f"({summary['paid_conversion_count']} paid conversions)</li>"
        f"</ul>"
        f"<p>Process in /admin/affiliates.</p>"
    )

    import asyncio
    svc = EmailService()

    async def _go() -> None:
        try:
            await svc.send(
                to=_ADMIN_NOTIFY_EMAIL,
                subject=subject,
                html=body,
                tags=["affiliate", "payout_request"],
            )
        finally:
            try:
                await svc.close()
            except Exception:
                pass

    # We're in an async route handler so the running loop exists.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_go())
    except RuntimeError:
        # Shouldn't happen — route handler always has a loop — but
        # defend against being called from a sync context.
        asyncio.run(_go())


# ── Admin panel ──────────────────────────────────────────────────────


@app.get("/admin/affiliates", response_class=HTMLResponse)
async def admin_affiliates_list(request: Request):
    """Admin list view. Also surfaces the pending-payouts queue."""
    user = _require_admin_user(request, page=True)
    if not user:
        return _denied_response(request)

    affiliates = da.list_affiliates(include_inactive=True)
    pending = da.list_affiliate_pending_payouts()

    import html as _html
    rows = []
    for a in affiliates:
        rows.append(
            f'<tr data-affiliate-id="{a["id"]}">'
            f'<td>{_html.escape(a["user_username"] or a["user_email"])}</td>'
            f'<td>{_html.escape(a["tier"])}</td>'
            f'<td>{int(round(a["commission_rate"] * 100))}%</td>'
            f'<td>{a["total_conversions"]}</td>'
            f'<td>£{a["total_earnings_pence"] / 100:.2f}</td>'
            f'<td>{"active" if a["is_active"] else "inactive"}</td>'
            f'<td><code>{_html.escape(a["affiliate_code"])}</code></td>'
            f'</tr>'
        )
    payout_rows = []
    for p in pending:
        payout_rows.append(
            f'<tr data-payout-for="{p["affiliate_id"]}">'
            f'<td>{_html.escape(p["user_username"] or p["user_email"])}</td>'
            f'<td>£{p["pending_pence"] / 100:.2f}</td>'
            f'<td>{p["unpaid_count"]}</td>'
            f'<td>{_html.escape(p["payout_email"] or "-")}</td>'
            f'<td><button class="mark-paid-btn" '
            f'data-affiliate-id="{p["affiliate_id"]}">Mark paid</button></td>'
            f'</tr>'
        )

    from admin_shell import render_admin_page
    return render_admin_page(
        request,
        "admin/affiliates.html",
        page_title="Affiliates",
        active_route="affiliates",
        breadcrumb=[("Admin", "/admin"), ("Affiliates", "/admin/affiliates")],
        total_affiliates=str(len(affiliates)),
        pending_payout_count=str(len(pending)),
        raw_affiliate_rows="\n".join(rows) or (
            '<tr><td colspan="7" style="opacity:0.6">No affiliates yet.</td></tr>'
        ),
        raw_payout_rows="\n".join(payout_rows) or (
            '<tr><td colspan="5" style="opacity:0.6">'
            'No pending payouts above threshold.</td></tr>'
        ),
    )


@app.post("/admin/affiliates")
async def admin_affiliates_create(request: Request):
    """Create an affiliate account for an existing user. Admin only."""
    admin = _require_admin_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    # Accept user by email OR user_id. The UI uses email; scripted
    # callers might have the user_id handy.
    user_email = (body.get("user_email") or "").strip().lower()
    user_id = body.get("user_id")
    if user_email:
        user_row = db.get_user_by_email(user_email)
        if not user_row:
            raise HTTPException(
                status_code=404,
                detail=f"No user with email {user_email}",
            )
        user_id = user_row["id"]
    if not user_id:
        raise HTTPException(status_code=400, detail="user_email or user_id required")

    try:
        commission_rate = float(body.get("commission_rate", 0.20))
        tier = str(body.get("tier", "partner")).strip()
        aff_id = da.create_affiliate_account(
            int(user_id),
            commission_rate=commission_rate,
            tier=tier,
            approved_by_admin_id=admin["user_id"],
            payout_method=(body.get("payout_method") or None),
            payout_email=(body.get("payout_email") or None),
            notes=(body.get("notes") or None),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    aff = da.get_affiliate_by_id(aff_id)
    log.info(
        "admin %s (id=%d) created affiliate id=%d for user_id=%d",
        admin.get("email"), admin.get("user_id"), aff_id, user_id,
    )
    return JSONResponse({
        "id": aff["id"],
        "affiliate_code": aff["affiliate_code"],
        "user_id": aff["user_id"],
        "tier": aff["tier"],
        "commission_rate": aff["commission_rate"],
    })


@app.patch("/admin/affiliates/{affiliate_id}")
async def admin_affiliates_update(request: Request, affiliate_id: int):
    admin = _require_admin_user(request)
    # SECURITY (H8): editing commission_rate / tier / is_active can be
    # abused by a level-1 admin to self-promote their own affiliate
    # account or hand a collaborator a 100% rate. Restrict mutation to
    # super-admin (level >= 2). Read endpoints remain at admin level 1.
    if (admin.get("admin_level") or 0) < 2:
        raise HTTPException(status_code=403, detail="super-admin required")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    try:
        ok = da.update_affiliate_account(
            affiliate_id,
            commission_rate=(
                float(body["commission_rate"]) if "commission_rate" in body else None
            ),
            tier=body.get("tier"),
            is_active=(
                bool(body["is_active"]) if "is_active" in body else None
            ),
            payout_method=body.get("payout_method"),
            payout_email=body.get("payout_email"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        raise HTTPException(status_code=404, detail="affiliate not found")

    aff = da.get_affiliate_by_id(affiliate_id)
    log.info(
        "admin %s updated affiliate id=%d", admin.get("email"), affiliate_id,
    )
    return JSONResponse({
        "id": aff["id"],
        "tier": aff["tier"],
        "commission_rate": aff["commission_rate"],
        "is_active": bool(aff["is_active"]),
        "payout_method": aff["payout_method"],
        "payout_email": aff["payout_email"],
    })


@app.post("/admin/affiliates/{affiliate_id}/payout")
async def admin_affiliates_mark_paid(request: Request, affiliate_id: int):
    """Flip every calculated-but-unpaid commission for this affiliate to
    commission_paid=1. Should be called AFTER the admin has sent the
    actual payment out-of-band (PayPal / wire / etc.).
    """
    admin = _require_admin_user(request)
    aff = da.get_affiliate_by_id(affiliate_id)
    if not aff:
        raise HTTPException(status_code=404, detail="affiliate not found")
    result = da.mark_affiliate_payout_complete(affiliate_id, admin["user_id"])
    log.info(
        "admin %s marked %d rows (£%.2f) paid for affiliate_id=%d",
        admin.get("email"),
        result["rows"],
        result["total_paid_pence"] / 100,
        affiliate_id,
    )
    return JSONResponse({
        "ok": True,
        "rows": result["rows"],
        "total_paid_gbp": round(result["total_paid_pence"] / 100, 2),
    })

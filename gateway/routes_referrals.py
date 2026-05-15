"""Private referral program + private leaderboard routes.

Lifted into a dedicated APIRouter so the new surface stays out of server.py's
increasingly-large monolith and can be tested / enabled independently.
server.py mounts this with a single `app.include_router(...)` line so there
is no circular dependency.

Routes registered here:

  Public (invitee):
    GET    /invite/{code}              -> invite_public.html
    GET    /api/invite/{code}
    POST   /api/invite/{code}/accept

  Authenticated (subscriber):
    GET    /settings/referrals         -> referrals.html
    GET    /api/referrals/me
    GET    /leaderboard                -> leaderboard.html
    GET    /api/leaderboard            ?period=all|90d|30d|7d
    POST   /api/leaderboard/participate
    DELETE /api/leaderboard/participate
    GET    /api/leaderboard/me
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
import db_referrals as dbr


log = logging.getLogger("routes.referrals")
router = APIRouter()


def _current_user(request: Request):
    """Resolve the current user via server.py's helper; imported lazily to
    avoid a circular import when server.py mounts this router at startup."""
    from server import current_user as _cu
    return _cu(request)


def _require_paid_user(request: Request):
    """Audit HIGH (2026-05-15) — leaderboard surfaces are paying-only.

    Mirrors routes_sharing._require_paid: returns the user dict if the
    caller has an active paid tier (trader/pro/enterprise) or is admin;
    returns None for free / trial / suspended / paused users so the
    caller can reply 402.

    Lazy import keeps this file free of a hard dependency on the queries
    package at module load (server.py is the one that wires routers).
    """
    user = _current_user(request)
    if not user:
        return None
    if user.get("is_admin"):
        return {**user, "tier": "pro"}
    from queries.subscriptions import get_user_subscription_tier
    tier = get_user_subscription_tier(user["user_id"])
    if tier in ("trader", "pro", "enterprise"):
        return {**user, "tier": tier}
    return None


def _band_user_count(n: int) -> str:
    """Audit HIGH (2026-05-15) — never publish the exact user count to
    paying customers. A banded approximation removes the side-channel
    that lets a logged-in user track headcount growth day-over-day.

    Bands:
      < 100    -> "<100"
      < 1_000  -> "100+"
      < 10_000 -> rounded to nearest 1_000 with a "+" suffix
      ≥ 10_000 -> rounded to nearest 10_000 with a "+" suffix
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "<100"
    if n < 100:
        return "<100"
    if n < 1_000:
        return "100+"
    if n < 10_000:
        rounded = (n // 1_000) * 1_000
        return f"{rounded:,}+"
    rounded = (n // 10_000) * 10_000
    return f"{rounded:,}+"


def _participate_rate_limited(user_id: int) -> bool:
    """Audit HIGH (2026-05-15) — cap participate POST/DELETE at 5 per
    hour per user so a paying account can't churn the leaderboard
    handle (or its opt-in state) into a spam vector. Uses the same
    Redis-backed sliding window as the rest of the gateway via
    server._is_rate_limited."""
    from server import _is_rate_limited as _irl
    return _irl(f"leaderboard-write:{user_id}", 5, 3600)


def _render_page(name: str, request: Request, **context):
    from server import render_page as _rp
    return _rp(name, request=request, **context)


def _display_name(row) -> str:
    if not row:
        return ""
    return row["username"] or (row["email"] or "").split("@")[0]


def _app_url() -> str:
    return os.environ.get("APP_URL", "https://narve.ai")


# ── Public invite flow ───────────────────────────────────────────────────────


@router.get("/invite/{code}", response_class=HTMLResponse)
async def public_invite_page(request: Request, code: str):
    """Public landing page for a referral invite link.

    Validates the referral code server-side so an invalid / suspended
    inviter renders a dead-end 'not valid' page rather than a usable form.
    The acceptance POST revalidates the code from scratch — this GET is
    rendering only.
    """
    referrer = dbr.get_user_by_referral_code(code)
    return _render_page(
        "invite_public",
        request,
        referrer_name=_display_name(referrer),
        code=(code or "").upper(),
        raw_valid="1" if referrer else "",
    )


@router.get("/api/invite/{code}")
async def api_invite_validate(code: str):
    """Public. Validates the referral code and returns the referrer's
    display name — invite page JS calls this on load to confirm the link
    is live before the invitee commits their email."""
    referrer = dbr.get_user_by_referral_code(code)
    if not referrer:
        return JSONResponse(
            {"valid": False, "error": "This invite link is not valid."},
            status_code=404,
        )
    return JSONResponse({
        "valid": True,
        "referrer_display_name": _display_name(referrer),
    })


@router.post("/api/invite/{code}/accept")
async def api_invite_accept(code: str, request: Request):
    """Public. Creates a single-use invite_token for this email, emails it,
    and records a pending Referral row.

    Rate-limited per IP (20/hour) and per email (3/day) so a malicious
    visitor can't hammer the email subsystem through this public endpoint.
    The invite code is the authorization — no session required.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    email = (body.get("email") or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse(
            {"error": "Enter a valid email address."},
            status_code=400,
        )

    ip = (request.client.host if request.client else "unknown") or "unknown"
    if db.rate_limit_hit(f"invite_accept:ip:{ip}", limit=20, window=3600):
        return JSONResponse({"error": "Too many requests."}, status_code=429)
    if db.rate_limit_hit(f"invite_accept:email:{email}", limit=3, window=86400):
        return JSONResponse(
            {"error": "Too many requests for that email."},
            status_code=429,
        )

    referrer = dbr.get_user_by_referral_code(code)
    if not referrer:
        return JSONResponse(
            {"error": "This invite link is not valid."},
            status_code=404,
        )

    # Don't re-invite an existing user. Send them to log in instead.
    existing = db.get_user_by_email(email)
    if existing:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "That email already has a narve.ai account. "
                    "Log in with your existing credentials."
                ),
            },
            status_code=409,
        )

    token_str = db.create_invite_token(
        note=f"via referral code {referrer['referral_code']}",
        target_email=email,
    )
    token_row = db.get_invite_token(token_str)
    token_id = token_row["id"] if token_row else None

    referral_id = dbr.create_referral(
        referrer_user_id=referrer["id"],
        referred_email=email,
        invite_token_id=token_id,
    )

    try:
        from jobs.email_jobs import enqueue_email
        await enqueue_email(
            to=email,
            template="referral_invite",
            context={
                "referrer_display_name": _display_name(referrer),
                # raw_ prefix bypasses HTML escaping in the email template
                # renderer (same convention as gateway render_page). Safe
                # here because `token_str` is secrets.token_urlsafe(…) output
                # — the character set is [A-Za-z0-9_-] only, so there's
                # nothing to escape. AUDIT #5 MED #4 asked us to annotate
                # every raw_* site; this is one of them.
                "raw_token": token_str,
                "app_url": _app_url(),
            },
            tags=["referral_invite"],
        )
    except Exception:
        log.exception("referral_invite email enqueue failed")
        # Token is already created and the referrer sees the pending
        # invitee in their list regardless of email state.

    return JSONResponse({"ok": True, "referral_id": referral_id})


# ── Authenticated referrer panel ─────────────────────────────────────────────


@router.get("/settings/referrals", response_class=HTMLResponse)
async def settings_referrals_page(request: Request):
    """Private referrer panel. Paid subscribers only."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    # Lazy-assign the user's referral_code the first time they visit.
    dbr.ensure_user_referral_code(user["user_id"])
    return _render_page(
        "referrals", request,
        breadcrumb=[
            ("narve.ai", "/dashboards"),
            ("Referrals", None),
        ],
    )


@router.get("/api/referrals/me")
async def api_referrals_me(request: Request):
    """Logged-in user's referral state. Always returns a code (minted on
    first call) + reward history + progress-to-next-milestone."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    user_id = user["user_id"]
    code = dbr.ensure_user_referral_code(user_id)
    stats = dbr.get_referral_stats(user_id)

    from backend.referrals import (
        progress_toward_next_reward,
        format_reward_label,
    )

    referrals = []
    for r in dbr.get_user_referrals(user_id):
        if r["reward_granted"]:
            status = "Reward granted"
        elif r["converted_to_paid"]:
            status = "Paying"
        elif r["referred_user_id"]:
            status = "Joined"
        else:
            status = "Invited"
        reward_label = None
        if r["reward_granted"] and (r["reward_type"] or "none") != "none":
            reward_label = format_reward_label(
                r["reward_type"],
                int(r["reward_months"] or 0),
                r["reward_tier"] or "",
            )
        referrals.append({
            "id": r["id"],
            "email": r["referred_email"] or r["referred_user_email"] or "—",
            "created_at": r["created_at"],
            "converted_to_paid": bool(r["converted_to_paid"]),
            "reward_granted": bool(r["reward_granted"]),
            "reward_label": reward_label,
            "status": status,
        })

    progress = progress_toward_next_reward(stats["total_converted"])
    return JSONResponse({
        "referral_code": code,
        "share_url": f"{_app_url()}/invite/{code}",
        "stats": stats,
        "progress": progress,
        "referrals": referrals,
    })


# ── Private leaderboard ──────────────────────────────────────────────────────


@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    """Private leaderboard page. Paying subscribers only."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/token", status_code=302)
    return _render_page(
        "leaderboard", request,
        breadcrumb=[
            ("narve.ai", "/dashboards"),
            ("Leaderboard", None),
        ],
    )


@router.get("/api/leaderboard")
async def api_leaderboard(
    request: Request, period: str = "all", limit: int = 100,
):
    """Opt-in users ranked by accuracy. Paid-only — same guard as the page.

    Audit HIGH (2026-05-15) — previously this endpoint only checked
    authentication, contradicting the docstring and leaking the
    competitive leaderboard to free / trial accounts. Now gated by
    ``_require_paid_user`` and replies 402 for non-paying callers. The
    user count returned is now banded (never the exact integer) and
    the per-row handle fallback no longer leaks raw ``user_id`` values
    when a participant opted in without setting a display name.
    """
    # Identity layer first so the 401/402 boundary is unambiguous.
    if not _current_user(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    user = _require_paid_user(request)
    if not user:
        return JSONResponse(
            {"error": "paid subscription required"},
            status_code=402,
        )

    if period not in ("all", "90d", "30d", "7d"):
        period = "all"
    if limit < 1 or limit > 500:
        limit = 100

    rows = dbr.get_leaderboard(period=period, limit=limit)
    participants = dbr.count_leaderboard_participants()
    my_rank = dbr.get_user_leaderboard_rank(user["user_id"], period=period)

    with db.conn() as c:
        total_users_row = c.execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE COALESCE(is_deleted, 0) = 0 AND COALESCE(suspended, 0) = 0"
        ).fetchone()
    raw_total = int(total_users_row["n"] if total_users_row else 0)

    out = []
    for i, r in enumerate(rows, start=1):
        # Never echo back the raw user_id — that's a stable internal
        # identifier and lets the leaderboard be used as a directory of
        # account numbers. If the participant never set a handle, render
        # them as "anonymous" instead.
        handle = (r["handle"] or "").strip() or "anonymous"
        out.append({
            "rank": i,
            "is_you": r["user_id"] == user["user_id"],
            "handle": handle,
            "total_predictions": int(r["total_predictions"] or 0),
            "correct_predictions": int(r["correct_predictions"] or 0),
            "accuracy": round(float(r["accuracy"]) * 100, 1)
                if r["accuracy"] is not None else None,
        })

    return JSONResponse({
        "period": period,
        "rows": out,
        "participants": participants,
        # Banded — clients should treat this as a display string, not
        # an integer. The old key name is preserved for client compat.
        "total_users_approx": _band_user_count(raw_total),
        "my_rank": my_rank,
    })


@router.post("/api/leaderboard/participate")
async def api_leaderboard_participate(request: Request):
    """Opt-in or update display name. Body: {display_name: str}.

    Audit HIGH (2026-05-15) — paid-only + per-user write rate limit
    (5/hour) so a hijacked or abusive account can't burn through
    handle changes / opt-flips on the public leaderboard.
    """
    if not _current_user(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    user = _require_paid_user(request)
    if not user:
        return JSONResponse(
            {"error": "paid subscription required"},
            status_code=402,
        )
    if _participate_rate_limited(user["user_id"]):
        return JSONResponse(
            {"error": "leaderboard write limit reached — try again later"},
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Display name on the public leaderboard — NFC-normalise, strip
    # zero-width / bidi control, reject null bytes. Length cap matches
    # the auth_register helper.
    from security.input_hygiene import clean_text
    try:
        display_name = clean_text(
            body.get("display_name"),
            max_len=40, required=True, field="display_name",
        )
    except Exception as exc:
        # Let FastAPI bubble HTTPException; other failures fall through
        # to the db helper's own "ok/error" contract.
        raise
    result = dbr.set_leaderboard_participation(
        user["user_id"],
        participate=True,
        display_name=display_name,
    )
    if not result["ok"]:
        return JSONResponse(
            {"error": result["error"]},
            status_code=409 if "taken" in (result["error"] or "") else 400,
        )
    return JSONResponse({"ok": True})


@router.delete("/api/leaderboard/participate")
async def api_leaderboard_opt_out(request: Request):
    """Opt out. Idempotent.

    Audit HIGH (2026-05-15) — same paywall + 5/hour write quota as the
    POST companion.
    """
    if not _current_user(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    user = _require_paid_user(request)
    if not user:
        return JSONResponse(
            {"error": "paid subscription required"},
            status_code=402,
        )
    if _participate_rate_limited(user["user_id"]):
        return JSONResponse(
            {"error": "leaderboard write limit reached — try again later"},
            status_code=429,
        )
    dbr.set_leaderboard_participation(user["user_id"], participate=False)
    return JSONResponse({"ok": True})


@router.get("/api/leaderboard/me")
async def api_leaderboard_me(request: Request):
    """Own opt-in state, for the /settings privacy panel."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    state = dbr.get_leaderboard_opt_in(user["user_id"])
    return JSONResponse(state)

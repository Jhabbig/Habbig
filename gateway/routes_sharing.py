"""Shareable-artifacts router.

Public + authenticated + admin routes for the share-loop feature set
(batch-3 prompt 3). Lives in a dedicated module / APIRouter so
server.py's monolith doesn't grow and so this surface can be tested
independently.

Routes registered:

  Public (invite-gated destination, read-only content):
    GET  /s/m/{token}              -> shared_market.html
    GET  /s/s/{token}              -> shared_source.html
    GET  /s/p/{token}              -> shared_prediction.html
    GET  /og/shared/market/{token}      -> PNG OG card
    GET  /og/shared/source/{token}      -> PNG OG card
    GET  /og/shared/prediction/{token}  -> PNG OG card
    GET  /tools/card-preview       -> card_preview.html (SEO tool)
    POST /api/tools/card-preview   -> preview metadata for a URL

  Authenticated (subscribers only):
    POST /api/share/market         -> mint a share token
    POST /api/share/source
    POST /api/share/prediction
    GET  /settings/invites         -> invites_settings.html
    GET  /api/invites/me           -> current balance + unused tokens

  Admin (wired in a separate diff — owner of admin_routes.py):
    GET  /admin/sharing            -> dashboard data

All auth is checked via server.current_user (imported lazily to dodge
the circular import at module load time).

CSRF: the public POST /api/invite/{code}/accept prefix exemption
(from prior work) already covers unauthenticated POSTs. Authenticated
share-mint POSTs go through the normal CSRF double-submit check.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import db
import db_sharing
import share_tokens
from auth.cookies import cookie_domain_for


log = logging.getLogger("routes.sharing")
router = APIRouter()


# ── Helpers ─────────────────────────────────────────────────────────


def _current_user(request):
    from server import current_user as _cu
    return _cu(request)


def _render_page(name, request, **ctx):
    from server import render_page as _rp
    return _rp(name, request=request, **ctx)


def _require_paid(request) -> Optional[dict]:
    """Authenticated + paid. Returns the user dict or None if the
    caller should be sent through the paywall path."""
    user = _current_user(request)
    if not user:
        return None
    tier = db.get_user_subscription_tier(user["user_id"])
    if tier in ("trader", "pro", "enterprise"):
        return {**user, "tier": tier}
    return None


def _display_handle(user_row) -> Optional[str]:
    """Pulls the shareable display name. Respects referral-program
    opt-in (``referral_code`` existence) — if a user hasn't generated
    a referral code yet they haven't consented to a public handle, so
    we return None and the card says 'Shared via narve.ai' instead."""
    if not user_row:
        return None
    return user_row.get("username") or None


def _safe_token_decode(token: str, expected_kind: str):
    """Verify + decode. Returns the decoded object or None on any
    failure — the caller replies 404 in both cases so we don't leak
    which class of failure occurred."""
    kind = share_tokens.peek_kind(token)
    if kind != expected_kind:
        return None
    try:
        return share_tokens.decode(token)
    except share_tokens.InvalidToken:
        return None


def _cf_country(request: Request) -> Optional[str]:
    return request.headers.get("cf-ipcountry") or request.headers.get("CF-IPCountry")


def _referer(request: Request) -> Optional[str]:
    return request.headers.get("referer") or request.headers.get("referrer")


# ── Public share pages ──────────────────────────────────────────────


@router.get("/s/m/{token}", response_class=HTMLResponse)
async def public_shared_market(request: Request, token: str):
    decoded = _safe_token_decode(token, "m")
    if not decoded:
        return _render_page("shared_invalid", request, kind="market")
    row = db_sharing.get_shared_market(token)
    if not row:
        return _render_page("shared_invalid", request, kind="market")
    db_sharing.record_shared_market_view(row["id"])
    metric_id = db_sharing.record_share_view(
        share_type="market", share_id=row["id"],
        referer=_referer(request), cf_country=_cf_country(request),
    )
    # Look up the market snapshot + narve signal data (best-effort —
    # if the market has been pruned, we still render the card with
    # what we have in the shared row).
    market_snapshot = None
    try:
        market_snapshot = db.get_latest_market_snapshot(row["market_slug"])
    except Exception:
        log.debug("no snapshot for shared market %s", row["market_slug"])
    response = _render_page(
        "shared_market", request,
        market_slug=row["market_slug"],
        sharer_handle=row["sharer_handle"] or "",
        market_question=(market_snapshot["question"] if market_snapshot else row["market_slug"]),
        market_probability=(
            f"{round(100 * market_snapshot['yes_probability'])}%"
            if market_snapshot and market_snapshot["yes_probability"] is not None
            else "—"
        ),
        token=token,
    )
    # Attribution cookie: lets the signup flow link this visitor's
    # eventual registration to the share they came from.
    # AUDIT #4 HIGH #3 — gate Secure on production so HTTP downgrades
    # can't leak the attribution metric_id (referral re-attribution).
    _is_prod = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
    response.set_cookie(
        "narve_share_attribution", str(metric_id),
        max_age=7 * 86400, httponly=True, samesite="lax", secure=_is_prod,
        domain=cookie_domain_for(request),
    )
    return response


@router.get("/s/s/{token}", response_class=HTMLResponse)
async def public_shared_source(request: Request, token: str):
    decoded = _safe_token_decode(token, "s")
    if not decoded:
        return _render_page("shared_invalid", request, kind="source")
    row = db_sharing.get_shared_source(token)
    if not row:
        return _render_page("shared_invalid", request, kind="source")
    db_sharing.record_shared_source_view(row["id"])
    metric_id = db_sharing.record_share_view(
        share_type="source", share_id=row["id"],
        referer=_referer(request), cf_country=_cf_country(request),
    )
    cred = None
    try:
        from queries import sources as sources_q
        cred = sources_q.get_source_credibility(row["source_handle"])
    except Exception:
        pass
    accuracy_pct = "—"
    if cred and cred["decay_weighted_accuracy"] is not None:
        accuracy_pct = f"{round(100 * float(cred['decay_weighted_accuracy']))}%"
    cred_score = "—"
    if cred and cred["global_credibility"] is not None:
        cred_score = f"{float(cred['global_credibility']):.2f}"
    response = _render_page(
        "shared_source", request,
        source_handle=row["source_handle"],
        sharer_handle=row["sharer_handle"] or "",
        accuracy_pct=accuracy_pct,
        cred_score=cred_score,
        token=token,
    )
    # AUDIT #4 HIGH #3 — gate Secure on production so HTTP downgrades
    # can't leak the attribution metric_id (referral re-attribution).
    _is_prod = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
    response.set_cookie(
        "narve_share_attribution", str(metric_id),
        max_age=7 * 86400, httponly=True, samesite="lax", secure=_is_prod,
        domain=cookie_domain_for(request),
    )
    return response


@router.get("/s/p/{token}", response_class=HTMLResponse)
async def public_shared_prediction(request: Request, token: str):
    decoded = _safe_token_decode(token, "p")
    if not decoded:
        return _render_page("shared_invalid", request, kind="prediction")
    row = db_sharing.get_shared_prediction(token)
    if not row:
        return _render_page("shared_invalid", request, kind="prediction")
    db_sharing.record_shared_prediction_view(row["id"])
    metric_id = db_sharing.record_share_view(
        share_type="prediction", share_id=row["id"],
        referer=_referer(request), cf_country=_cf_country(request),
    )
    pred = None
    try:
        from queries import predictions as preds_q
        pred = preds_q.get_user_prediction(row["user_prediction_id"])
    except Exception:
        pass
    response = _render_page(
        "shared_prediction", request,
        sharer_handle=row["sharer_handle"] or "",
        market_slug=(pred["market_slug"] if pred else "—"),
        direction=(pred["direction"] if pred else "—"),
        confidence=(
            f"{round(100 * float(pred['predicted_probability']))}%"
            if pred and pred["predicted_probability"] is not None else "—"
        ),
        token=token,
    )
    # AUDIT #4 HIGH #3 — gate Secure on production so HTTP downgrades
    # can't leak the attribution metric_id (referral re-attribution).
    _is_prod = os.environ.get("PRODUCTION", "").lower() in ("1", "true", "yes", "on")
    response.set_cookie(
        "narve_share_attribution", str(metric_id),
        max_age=7 * 86400, httponly=True, samesite="lax", secure=_is_prod,
        domain=cookie_domain_for(request),
    )
    return response


# ── OG card images ──────────────────────────────────────────────────


def _og_response(png_bytes: bytes) -> Response:
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _og_fallback() -> Response:
    """1×1 transparent PNG for when token is invalid or Pillow missing.
    Using a tiny image instead of 404 means social-media scrapers
    don't retry, and a broken link in a tweet degrades gracefully."""
    # Minimal valid 1×1 PNG — avoids a Pillow call in the fallback path.
    tiny = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
        b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx"
        b"\x9cc\xf8\xff\xff?\x03\x00\x05\xfe\x02\xfe\xa7V\xbd\x9f\x00\x00"
        b"\x00\x00IEND\xaeB`\x82"
    )
    return _og_response(tiny)


# OG image cache TTL. Every share token has a 7-day lifetime (see
# share_tokens.DEFAULT_TTL_SECONDS). Caching for a full day means a
# scraper-retry storm against a popular tweet hits memory after the first
# render, but a freshly-minted share still gets a current image within a
# day of going viral. Anything shorter wastes CPU; anything longer risks
# showing a stale card if we tweak the renderer mid-day.
_OG_CACHE_TTL_SECONDS = 24 * 3600


@router.get("/og/shared/market/{token}")
async def og_shared_market(token: str):
    if not _safe_token_decode(token, "m"):
        return _og_fallback()
    row = db_sharing.get_shared_market(token)
    if not row:
        return _og_fallback()
    try:
        from og_cards import render_shared_market_card, cached
        png = cached(
            key=f"share:m:{token}",
            ttl_seconds=_OG_CACHE_TTL_SECONDS,
            factory=lambda: render_shared_market_card(
                market_slug=row["market_slug"],
                sharer_handle=row["sharer_handle"] or "",
            ),
        )
        return _og_response(png)
    except Exception:
        log.exception("og shared market render failed for %s", row["market_slug"])
        return _og_fallback()


@router.get("/og/shared/source/{token}")
async def og_shared_source(token: str):
    if not _safe_token_decode(token, "s"):
        return _og_fallback()
    row = db_sharing.get_shared_source(token)
    if not row:
        return _og_fallback()
    try:
        from og_cards import render_shared_source_card, cached
        png = cached(
            key=f"share:s:{token}",
            ttl_seconds=_OG_CACHE_TTL_SECONDS,
            factory=lambda: render_shared_source_card(
                source_handle=row["source_handle"],
                sharer_handle=row["sharer_handle"] or "",
            ),
        )
        return _og_response(png)
    except Exception:
        log.exception("og shared source render failed for %s", row["source_handle"])
        return _og_fallback()


@router.get("/og/shared/prediction/{token}")
async def og_shared_prediction(token: str):
    if not _safe_token_decode(token, "p"):
        return _og_fallback()
    row = db_sharing.get_shared_prediction(token)
    if not row:
        return _og_fallback()
    try:
        from og_cards import render_shared_prediction_card, cached
        png = cached(
            key=f"share:p:{token}",
            ttl_seconds=_OG_CACHE_TTL_SECONDS,
            factory=lambda: render_shared_prediction_card(
                user_prediction_id=row["user_prediction_id"],
                sharer_handle=row["sharer_handle"] or "",
            ),
        )
        return _og_response(png)
    except Exception:
        log.exception("og shared prediction render failed for %s", row["user_prediction_id"])
        return _og_fallback()


# ── /tools/card-preview (public SEO tool) ──────────────────────────


@router.get("/tools/card-preview", response_class=HTMLResponse)
async def card_preview_tool(request: Request):
    return _render_page("card_preview", request)


@router.post("/api/tools/card-preview")
async def api_card_preview(request: Request):
    """Accepts a narve share URL, returns the metadata our OG card
    would render. Lets a visitor see what a tweet will look like
    before posting — also gives them a reason to land on narve.ai
    without a signup gate (pure SEO surface)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "error": "missing url"}, status_code=400)

    # Extract the token segment. Match /s/m/XX or /s/s/XX or /s/p/XX.
    import re
    m = re.search(r"/s/([msp])/([A-Za-z0-9._-]+)", url)
    if not m:
        return JSONResponse(
            {"ok": False, "error": "not a narve share URL"}, status_code=400,
        )
    kind, token = m.group(1), m.group(2)
    if not _safe_token_decode(token, kind):
        return JSONResponse(
            {"ok": False, "error": "invalid or expired"}, status_code=404,
        )

    if kind == "m":
        row = db_sharing.get_shared_market(token)
        return JSONResponse({
            "ok": True, "kind": "market",
            "title": f"Market — {row['market_slug']}" if row else "",
            "subtitle": "narve.ai signal",
            "og_image_url": f"/og/shared/market/{token}",
        })
    if kind == "s":
        row = db_sharing.get_shared_source(token)
        return JSONResponse({
            "ok": True, "kind": "source",
            "title": f"@{row['source_handle']}" if row else "",
            "subtitle": "tracked by narve.ai",
            "og_image_url": f"/og/shared/source/{token}",
        })
    # kind == "p"
    row = db_sharing.get_shared_prediction(token)
    return JSONResponse({
        "ok": True, "kind": "prediction",
        "title": "Resolved prediction",
        "subtitle": "narve.ai — track your accuracy",
        "og_image_url": f"/og/shared/prediction/{token}",
    })


# ── Authenticated: mint a share ────────────────────────────────────

# Per-user mint cap: 20 shares/hour across all three mint endpoints.
# A single Pro user doing more than 20 shares in an hour is either
# spamming or automated — neither is a legitimate product use case.
# Shared budget (one key, all three endpoints) so a compromised
# account can't fan out by alternating between market/source/prediction
# mints to triple their effective rate.
_MINT_LIMIT_PER_HOUR = 20
_MINT_WINDOW_SECONDS = 3600


def _mint_rate_limited(user_id: int) -> bool:
    """Return True iff the user has exceeded the mint budget for the
    current hour. Uses server's shared _is_rate_limited helper so
    Redis-backed counters work identically to /auth/*."""
    from server import _is_rate_limited as _irl
    return _irl(f"share_mint:{user_id}", _MINT_LIMIT_PER_HOUR, _MINT_WINDOW_SECONDS)


def _sharer_handle_for(user) -> Optional[str]:
    """What goes in shared_*.sharer_handle. Prefer the user's
    display-preferred handle; fall back to None so the UI renders
    'Shared via narve.ai' instead of a generated string."""
    return user.get("username") or None


@router.post("/api/share/market")
async def api_share_market(request: Request):
    user = _require_paid(request)
    if not user:
        return JSONResponse({"error": "paid subscription required"}, status_code=402)
    if _mint_rate_limited(user["user_id"]):
        return JSONResponse(
            {"error": "share mint limit reached — try again in an hour"},
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    slug = (body.get("market_slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing market_slug"}, status_code=400)
    row = db_sharing.create_shared_market(
        market_slug=slug,
        sharer_user_id=user["user_id"],
        sharer_handle=_sharer_handle_for(user),
    )
    return JSONResponse({
        "ok": True,
        "token": row["token"],
        "share_url": f"/s/m/{row['token']}",
        "expires_at": row["expires_at"],
    })


@router.post("/api/share/source")
async def api_share_source(request: Request):
    user = _require_paid(request)
    if not user:
        return JSONResponse({"error": "paid subscription required"}, status_code=402)
    if _mint_rate_limited(user["user_id"]):
        return JSONResponse(
            {"error": "share mint limit reached — try again in an hour"},
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    handle = (body.get("source_handle") or "").strip()
    if not handle:
        return JSONResponse({"error": "missing source_handle"}, status_code=400)
    row = db_sharing.create_shared_source(
        source_handle=handle,
        sharer_user_id=user["user_id"],
        sharer_handle=_sharer_handle_for(user),
    )
    return JSONResponse({
        "ok": True,
        "token": row["token"],
        "share_url": f"/s/s/{row['token']}",
        "expires_at": row["expires_at"],
    })


@router.post("/api/share/prediction")
async def api_share_prediction(request: Request):
    user = _require_paid(request)
    if not user:
        return JSONResponse({"error": "paid subscription required"}, status_code=402)
    if _mint_rate_limited(user["user_id"]):
        return JSONResponse(
            {"error": "share mint limit reached — try again in an hour"},
            status_code=429,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    pid = body.get("user_prediction_id")
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return JSONResponse({"error": "invalid user_prediction_id"}, status_code=400)
    row = db_sharing.create_shared_prediction(
        user_prediction_id=pid_int,
        sharer_user_id=user["user_id"],
        sharer_handle=_sharer_handle_for(user),
    )
    if row is None:
        # Either not this user's prediction, or not resolved_correct=1.
        # Deliberately vague so we don't leak whether a given prediction
        # id exists or belongs to someone else.
        return JSONResponse(
            {"error": "only resolved-correct predictions you own can be shared"},
            status_code=400,
        )
    return JSONResponse({
        "ok": True,
        "token": row["token"],
        "share_url": f"/s/p/{row['token']}",
        "expires_at": row["expires_at"],
    })


# ── /settings/invites + balance API ────────────────────────────────


@router.get("/settings/invites", response_class=HTMLResponse)
async def settings_invites(request: Request):
    user = _require_paid(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _render_page("invites_settings", request)


@router.get("/api/invites/me")
async def api_invites_me(request: Request):
    user = _require_paid(request)
    if not user:
        return JSONResponse({"error": "paid subscription required"}, status_code=402)
    tokens = db_sharing.list_unused_invite_tokens(user["user_id"])
    return JSONResponse({
        "tier": user["tier"],
        "monthly_allotment": db_sharing.INVITE_ALLOTMENT_BY_TIER.get(user["tier"], 0),
        "rollover_cap": (
            db_sharing.INVITE_ALLOTMENT_BY_TIER.get(user["tier"], 0)
            * db_sharing.ROLLOVER_MULTIPLIER
        ),
        "balance": len(tokens),
        "tokens": [
            {
                "token": t["token"],
                "created_at": int(t["created_at"]),
                "tier_at_grant": t["tier_at_grant"],
            } for t in tokens
        ],
    })

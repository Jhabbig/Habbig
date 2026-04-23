"""Public developer API v1 — /api/public/v1/*

Every endpoint:
  1. Goes through `verify_api_key` (adds request.state.api_key)
  2. Returns JSON
  3. Wraps the outbound payload through `sign_if_available` so any
     screenshot/scrape is attributable back to the key-owner

Data-access is delegated to the existing query modules — this router is
a thin mapping layer from HTTP → db helpers → JSON shape. Nothing here
should be doing SQL itself.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import db

from .auth import (
    require_scope,
    sign_if_available,
    verify_api_key,
)


log = logging.getLogger("api.public.v1")

router = APIRouter(prefix="/api/public/v1", tags=["public-api-v1"])


# ── Helpers ─────────────────────────────────────────────────────────────


def _rows(iterable) -> list[dict]:
    out = []
    for r in iterable or []:
        try:
            out.append({k: r[k] for k in r.keys()})
        except Exception:
            try:
                out.append(dict(r))
            except Exception:
                pass
    return out


def _ok(request: Request, endpoint: str, payload):
    """Final response wrapper: sign + attach rate-limit headers + JSONResponse.

    Signing runs through sign_if_available (forensic attribution). Rate-
    limit headers surface the caller's position in the current bucket so
    clients can self-regulate without polling /usage. The 429 path in
    auth.py surfaces the same four headers; mirroring them on 2xx keeps
    client state machines simple.
    """
    key = request.state.api_key
    user_id = key["user_id"]
    signed = sign_if_available(user_id, payload, endpoint)

    limit = int(key["rate_limit_hour"] or 0)
    used = int(key["usage_this_hour"] or 0)
    remaining = max(0, limit - used) if limit else 0
    bucket_end = int(key["hour_bucket"]) + 3600
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(bucket_end),
        "X-Narve-Key-Prefix": key.get("key_prefix", ""),
    }
    return JSONResponse(signed, headers=headers)


def _clamp(limit, default: int, hard_max: int) -> int:
    try:
        v = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        v = default
    return max(1, min(v, hard_max))


# ── Markets ─────────────────────────────────────────────────────────────


@router.get("/markets")
async def v1_markets(request: Request, q: str = "", limit: int = 50):
    verify_api_key(request)
    lim = _clamp(limit, 50, 200)
    try:
        import queries.markets as qm
        rows = qm.search_markets(q or "", limit=lim) if q else []
    except Exception as exc:
        log.warning("public markets search failed: %s", exc)
        rows = []
    return _ok(request, "public.markets", {"markets": _rows(rows), "limit": lim, "q": q})


@router.get("/markets/{slug}")
async def v1_market_detail(request: Request, slug: str):
    verify_api_key(request)
    try:
        import queries.markets as qm
        snap = qm.get_latest_market_snapshot(slug)
        if snap is None:
            raise HTTPException(404, f"Market {slug} not found")
        return _ok(request, "public.market_detail", {"market": dict(snap)})
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("public market_detail failed slug=%s: %s", slug, exc)
        raise HTTPException(500, "market lookup failed")


@router.get("/markets/{slug}/predictions")
async def v1_market_predictions(request: Request, slug: str, limit: int = 100):
    verify_api_key(request)
    lim = _clamp(limit, 100, 500)
    try:
        import queries.predictions as qp
        rows = qp.get_predictions_for_market(slug)[:lim]
    except Exception as exc:
        log.warning("public market_predictions failed: %s", exc)
        rows = []
    return _ok(request, "public.market_predictions",
               {"market_slug": slug, "predictions": _rows(rows), "limit": lim})


@router.get("/markets/{slug}/history")
async def v1_market_history(request: Request, slug: str, limit: int = 500):
    verify_api_key(request)
    lim = _clamp(limit, 500, 2000)
    try:
        import queries.markets as qm
        rows = qm.get_market_history(slug, limit=lim)
    except Exception as exc:
        log.warning("public market_history failed: %s", exc)
        rows = []
    return _ok(request, "public.market_history",
               {"market_slug": slug, "history": _rows(rows), "limit": lim})


# ── Sources ─────────────────────────────────────────────────────────────


@router.get("/sources")
async def v1_sources(request: Request, limit: int = 100, offset: int = 0):
    verify_api_key(request)
    lim = _clamp(limit, 100, 500)
    try:
        import queries.sources as qs
        all_rows = qs.list_all_source_credibilities()
    except Exception as exc:
        log.warning("public sources list failed: %s", exc)
        all_rows = []
    page = _rows(all_rows[offset:offset + lim])
    return _ok(request, "public.sources",
               {"sources": page, "total": len(all_rows), "limit": lim, "offset": offset})


@router.get("/sources/{handle}")
async def v1_source_detail(request: Request, handle: str):
    verify_api_key(request)
    try:
        import queries.sources as qs
        row = qs.get_source_credibility(handle)
    except Exception as exc:
        log.warning("public source_detail failed: %s", exc)
        row = None
    if row is None:
        raise HTTPException(404, f"Source @{handle} not found")
    return _ok(request, "public.source_detail", {"source": dict(row)})


@router.get("/sources/{handle}/predictions")
async def v1_source_predictions(request: Request, handle: str, limit: int = 100):
    verify_api_key(request)
    lim = _clamp(limit, 100, 500)
    # There isn't a dedicated per-source predictions helper in the query
    # layer yet, so we fall back to the source-credibility join. Returns
    # an empty list gracefully if the helper is ever refactored away.
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT * FROM predictions WHERE source_handle = ? "
                "ORDER BY extracted_at DESC LIMIT ?",
                (handle, lim),
            ).fetchall()
    except Exception as exc:
        log.warning("public source_predictions failed handle=%s: %s", handle, exc)
        rows = []
    return _ok(request, "public.source_predictions",
               {"handle": handle, "predictions": _rows(rows), "limit": lim})


@router.get("/sources/{handle}/history")
async def v1_source_history(request: Request, handle: str, limit: int = 200):
    verify_api_key(request)
    lim = _clamp(limit, 200, 1000)
    try:
        with db.conn() as c:
            rows = c.execute(
                "SELECT source_handle, global_credibility, snapshot_at "
                "FROM credibility_snapshots "
                "WHERE source_handle = ? ORDER BY snapshot_at DESC LIMIT ?",
                (handle, lim),
            ).fetchall()
    except Exception as exc:
        log.warning("public source_history failed handle=%s: %s", handle, exc)
        rows = []
    return _ok(request, "public.source_history",
               {"handle": handle, "history": _rows(rows), "limit": lim})


# ── Feed / Best bets / Calendar ─────────────────────────────────────────


@router.get("/feed")
async def v1_feed(request: Request, limit: int = 100, category: Optional[str] = None):
    verify_api_key(request)
    lim = _clamp(limit, 100, 500)
    try:
        import queries.predictions as qp
        rows = qp.list_recent_predictions(limit=lim, category=category)
    except Exception as exc:
        log.warning("public feed failed: %s", exc)
        rows = []
    return _ok(request, "public.feed",
               {"feed": _rows(rows), "limit": lim, "category": category})


@router.get("/best-bets")
async def v1_best_bets(request: Request, limit: int = 20):
    """Currently-top edge opportunities.

    There's no dedicated best-bets helper in the query layer (§7 on the
    roadmap), so for v1 we surface the most-recent predictions with
    category filter = 'all' and let clients compute edge themselves from
    the paired market snapshot. When the dedicated helper lands we'll
    swap the body here without changing the response shape.
    """
    verify_api_key(request)
    lim = _clamp(limit, 20, 100)
    try:
        import queries.predictions as qp
        rows = qp.list_recent_predictions(limit=lim)
    except Exception as exc:
        log.warning("public best-bets failed: %s", exc)
        rows = []
    return _ok(request, "public.best_bets",
               {"best_bets": _rows(rows), "limit": lim,
                "note": "v1 surfaces recent predictions; edge helper in roadmap"})


@router.get("/calendar")
async def v1_calendar(request: Request, limit: int = 100):
    """Upcoming market resolutions. Reuses the user-prediction helpers
    because calendar data is identical to 'markets resolving soon'."""
    verify_api_key(request)
    lim = _clamp(limit, 100, 500)
    try:
        # Markets with a known deadline, ordered by next resolution.
        # Best-effort — an unpopulated deadline field means we fall back
        # to most-recent markets.
        with db.conn() as c:
            rows = c.execute(
                "SELECT market_slug, market_question, deadline_at, category "
                "FROM predictions "
                "WHERE deadline_at IS NOT NULL AND deadline_at > strftime('%s','now') "
                "GROUP BY market_slug "
                "ORDER BY deadline_at ASC LIMIT ?",
                (lim,),
            ).fetchall()
    except Exception as exc:
        log.warning("public calendar failed: %s", exc)
        rows = []
    return _ok(request, "public.calendar",
               {"calendar": _rows(rows), "limit": lim})


# ── Write: POST /predictions ────────────────────────────────────────────


@router.get("/predictions/{prediction_id}")
async def v1_get_prediction(request: Request, prediction_id: int):
    """Fetch one of the caller's own predictions. Also returns any
    prediction that the owner explicitly marked `is_public=1`. Anyone
    else's private prediction returns 404 — we deliberately don't leak
    existence.

    Pairs with POST /predictions so a client-side bot can round-trip:
    create → fetch → (later) check resolution status.
    """
    verify_api_key(request)
    key = request.state.api_key
    try:
        import queries.predictions as qp
        row = qp.get_user_prediction(prediction_id)
    except Exception as exc:
        log.warning("public get_user_prediction failed id=%s: %s", prediction_id, exc)
        row = None
    if row is None:
        raise HTTPException(404, "Prediction not found")

    is_owner = row["user_id"] == key["user_id"]
    if not is_owner and not row["is_public"]:
        raise HTTPException(404, "Prediction not found")

    payload = {
        "prediction": {
            k: row[k] for k in row.keys()
        },
        "is_owner": is_owner,
    }
    # Anonymise non-owner responses to respect the author's is_anonymous
    # flag — the public-profile page does the same scrub.
    if not is_owner and row["is_anonymous"]:
        payload["prediction"]["user_id"] = None
    return _ok(request, "public.get_prediction", payload)


@router.post("/predictions", dependencies=[Depends(require_scope("write"))])
async def v1_create_prediction(request: Request):
    """Submit a user prediction. Requires `write` scope.

    Body is JSON (not form): {market_slug, predicted_outcome (YES|NO),
    predicted_probability (0..1 or 0..100), reasoning?, category?,
    market_question?, market_price?, is_public?, is_anonymous?}.
    """
    key = request.state.api_key
    user_id = key["user_id"]

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON body must be an object")

    slug = (body.get("market_slug") or "").strip()
    outcome = (body.get("predicted_outcome") or "").strip().upper()
    if not slug or len(slug) > 200:
        raise HTTPException(400, "market_slug required")
    if outcome not in ("YES", "NO"):
        raise HTTPException(400, "predicted_outcome must be YES or NO")

    try:
        prob_raw = float(body.get("predicted_probability"))
    except (TypeError, ValueError):
        raise HTTPException(400, "predicted_probability must be a number")
    prob = prob_raw / 100.0 if prob_raw > 1.0 else prob_raw
    if not 0.0 <= prob <= 1.0:
        raise HTTPException(400, "predicted_probability must be between 0 and 1 (or 0 and 100)")

    market_price = body.get("market_price")
    if market_price is not None:
        try:
            mpv = float(market_price)
            market_price = mpv / 100.0 if mpv > 1.0 else mpv
        except (TypeError, ValueError):
            market_price = None

    try:
        import queries.predictions as qp
        existing = qp.get_active_user_prediction(user_id, slug)
        if existing:
            raise HTTPException(409, "You already have an active prediction on this market")
        pid = qp.create_user_prediction(
            user_id=user_id,
            market_slug=slug,
            market_question=(body.get("market_question") or "")[:500],
            category=(body.get("category") or "other")[:32],
            predicted_outcome=outcome,
            predicted_probability=prob,
            reasoning=(body.get("reasoning") or None),
            market_price_at_prediction=market_price,
            is_public=bool(body.get("is_public")),
            is_anonymous=bool(body.get("is_anonymous")),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("public create_user_prediction failed uid=%s slug=%s: %s",
                      user_id, slug, exc)
        raise HTTPException(500, "Could not save prediction")

    return _ok(request, "public.create_prediction",
               {"prediction_id": pid, "status": "ok"})


# ── Usage ───────────────────────────────────────────────────────────────


@router.get("/usage")
async def v1_usage(request: Request):
    verify_api_key(request)
    key = request.state.api_key
    return _ok(request, "public.usage", {
        "key_id": key["id"],
        "key_prefix": key["key_prefix"],
        "name": key["name"],
        "scopes": sorted(key["scopes"]),
        "rate_limit_hour": key["rate_limit_hour"],
        "requests_this_hour": key["usage_this_hour"],
        "hour_bucket_start": key["hour_bucket"],
        "bucket_resets_at": key["hour_bucket"] + 3600,
    })

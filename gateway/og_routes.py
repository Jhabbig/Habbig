"""Public OG card routes.

``og_cards.py`` already owns the PIL rendering + in-memory cache. This
module wires public ``/og/*`` routes that the canonical base template
(static/_base.html) can reference as `og_image`, so every shared URL
renders with a branded monochrome card without any per-page asset
bookkeeping.

Paths — all return image/png (the card renderer paints onto a PNG
buffer; Pillow's WebP encode costs enough CPU per card that returning
PNG + the existing 1-hour in-process cache is faster at our traffic).

    /og/default              Site-wide fallback.
    /og/pricing              "Plans from £75/month" card.
    /og/calendar             Upcoming market resolutions card.
    /og/source/{handle}      Per-source credibility card.
    /og/market/{slug}        Per-market narve-vs-market probabilities.

The route for per-user shared tokens (/og/shared/*) lives in
routes_sharing.py — different auth model + lifecycle, kept separate.

All cards cache for an hour in-process (``og_cards.cached()``). Cache
headers also send ``Cache-Control: public, max-age=3600`` so the
browser + Cloudflare hold onto the bytes too.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response


log = logging.getLogger("gateway.og_routes")

router = APIRouter()

_CACHE_TTL = 3600
_HEADERS = {
    "Cache-Control": f"public, max-age={_CACHE_TTL}, stale-while-revalidate=86400",
    "Content-Type": "image/png",
}


def _png(buf: bytes) -> Response:
    return Response(content=buf, media_type="image/png", headers=_HEADERS)


@router.get("/og/default")
async def og_default() -> Response:
    """Site-wide fallback card. Referenced by _base.html as the default
    og_image when a page hasn't set a more specific variant.
    """
    from og_cards import default_card, cached
    buf = cached("og:default", _CACHE_TTL, default_card)
    return _png(buf)


@router.get("/og/pricing")
async def og_pricing() -> Response:
    from og_cards import pricing_card, cached
    buf = cached("og:pricing", _CACHE_TTL, pricing_card)
    return _png(buf)


@router.get("/og/calendar")
async def og_calendar() -> Response:
    from og_cards import calendar_card, cached
    buf = cached("og:calendar", _CACHE_TTL, calendar_card)
    return _png(buf)


@router.get("/og/source/{handle}")
async def og_source(handle: str) -> Response:
    """Per-source card. Pulls credibility + accuracy + prediction count
    from the source credibility table. Unknown sources 404."""
    import db
    from og_cards import source_card, cached

    row = None
    try:
        row = db.get_source_credibility(handle)
    except Exception as e:
        log.warning("og_source lookup failed for %s: %s", handle, e)

    if not row:
        raise HTTPException(status_code=404, detail="Source not found")

    # sqlite3.Row has no .get(); access by index + defensive-cast.
    try:
        cred = float(row["global_credibility"]) if row["global_credibility"] is not None else None
    except Exception:
        cred = None
    try:
        acc = (
            float(row["decay_weighted_accuracy"])
            if row["decay_weighted_accuracy"] is not None
            else None
        )
    except Exception:
        acc = None
    try:
        count = int(row["total_predictions"] or 0)
    except Exception:
        count = 0

    cache_key = f"og:source:{handle}"
    buf = cached(
        cache_key,
        _CACHE_TTL,
        lambda: source_card(handle, cred, acc, count),
    )
    return _png(buf)


@router.get("/og/market/{slug:path}")
async def og_market(slug: str) -> Response:
    """Per-market card showing narve vs market probabilities."""
    import db
    from og_cards import market_card, cached

    snap = None
    try:
        snap = db.get_latest_market_snapshot(slug)
    except Exception as e:
        log.warning("og_market lookup failed for %s: %s", slug, e)

    if not snap:
        raise HTTPException(status_code=404, detail="Market not found")

    question = snap["market_question"] if "market_question" in snap.keys() else slug
    yes_price = None
    try:
        yes_price = float(snap["yes_price"]) if snap["yes_price"] is not None else None
    except Exception:
        pass

    # narve's consensus probability lives on a separate helper; if it's
    # not available we just render the market price alone.
    narve_prob: Any = None
    try:
        from db import calculate_betyc_probability
        preds = db.get_predictions_for_market(f"poly:{slug}") or []
        if preds:
            res = calculate_betyc_probability([dict(p) for p in preds])
            narve_prob = res.get("betyc_yes_probability")
    except Exception:
        narve_prob = None

    cache_key = f"og:market:{slug}"
    # Infer the platform prefix from the slug — polymarket slugs are
    # raw strings; kalshi: explicitly prefixed. Fall back to "market"
    # when we can't tell so the footer never reads "None".
    if slug.startswith("kalshi:"):
        platform = "Kalshi"
    elif slug.startswith("poly:") or "/" not in slug:
        platform = "Polymarket"
    else:
        platform = "market"
    buf = cached(
        cache_key,
        _CACHE_TTL,
        lambda: market_card(
            title=question,
            market_price=yes_price,
            narve_price=narve_prob if isinstance(narve_prob, (int, float)) else None,
            platform=platform,
        ),
    )
    return _png(buf)


def register(app) -> None:
    """Mount the router on the main FastAPI app.

    Keeps the registration side-effect explicit so server.py's import
    graph doesn't carry an import-time mount through this module — the
    rest of the codebase uses the same register() convention.
    """
    app.include_router(router)

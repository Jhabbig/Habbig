"""Versioned developer API for narve.ai (F12).

Bearer-token authentication via API keys. Rate-limited per key tier.
Designed for quant funds, bot builders, and researchers who want
programmatic access to credibility scores, predictions, and edge data.

Mount in server.py:
    from api_v1 import router as api_v1_router
    app.include_router(api_v1_router)
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db

log = logging.getLogger("api.v1")

router = APIRouter(prefix="/api/v1", tags=["v1"])


# ── API key helpers ─────────────────────────────────────────────────────────


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(user_id: int, name: str = "", tier: str = "standard") -> tuple[str, int]:
    """Create a new API key. Returns (raw_key, key_id).

    The raw key is shown ONCE. Only the hash is stored.
    """
    raw_key = f"narve_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    prefix = raw_key[:12]
    rate_limit = 10000 if tier == "enterprise" else 1000
    now = int(time.time())

    with db.conn() as c:
        cur = c.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, user_id, name, tier, rate_limit_hour, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key_hash, prefix, user_id, name, tier, rate_limit, now),
        )
        key_id = cur.lastrowid

    return raw_key, key_id


def _validate_key(request: Request) -> dict:
    """Validate the Bearer token. Returns the api_key row dict.

    Raises HTTPException on failure.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "API key required. Use: Authorization: Bearer <key>")

    raw_key = auth[7:]
    key_hash = _hash_key(raw_key)

    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
        ).fetchone()

    if not row:
        raise HTTPException(401, "Invalid API key")
    if row["revoked_at"]:
        raise HTTPException(401, "API key has been revoked")

    # Rate limit
    from security.rate_limiter import limiter
    allowed, remaining, retry_after = limiter.check(
        f"apiv1:{row['id']}", row["rate_limit_hour"], 3600
    )
    if not allowed:
        raise HTTPException(
            429, "Rate limit exceeded",
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(row["rate_limit_hour"])},
        )

    # Touch last_used_at
    with db.conn() as c:
        c.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (int(time.time()), row["id"]),
        )

    return dict(row)


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/sources")
async def v1_list_sources(request: Request, limit: int = 100, offset: int = 0):
    """All sources with credibility scores."""
    _validate_key(request)
    sources = db.list_all_source_credibilities()
    page = sources[offset:offset + min(limit, 500)]
    return JSONResponse({
        "sources": [
            {
                "handle": s["source_handle"],
                "global_credibility": s["global_credibility"],
                "accuracy_unlocked": bool(s["accuracy_unlocked"]),
                "decay_weighted_accuracy": s["decay_weighted_accuracy"],
                "total_predictions": s["total_predictions"],
                "correct_predictions": s["correct_predictions"],
                "categories_active": s["categories_active"],
            }
            for s in page
        ],
        "total": len(sources),
        "limit": limit,
        "offset": offset,
    })


@router.get("/sources/{handle}")
async def v1_source_detail(request: Request, handle: str):
    """Full source profile with calibration data."""
    _validate_key(request)
    cred = db.get_source_credibility(handle)
    if not cred:
        raise HTTPException(404, "Source not found")

    cats = db.get_all_category_credibilities(handle)
    snaps = db.get_credibility_snapshots(handle, 10)
    cal = db.get_source_calibration(handle)

    return JSONResponse({
        "handle": handle,
        "global_credibility": cred["global_credibility"],
        "accuracy_unlocked": bool(cred["accuracy_unlocked"]),
        "decay_weighted_accuracy": cred["decay_weighted_accuracy"],
        "total_predictions": cred["total_predictions"],
        "correct_predictions": cred["correct_predictions"],
        "categories": [
            {"category": c["category"], "credibility": c["category_credibility"],
             "prediction_count": c["prediction_count"], "correct_count": c["correct_count"]}
            for c in cats
        ],
        "snapshots": [{"credibility": s["global_credibility"], "at": s["snapshot_at"]} for s in snaps],
        "calibration": cal,
    })


@router.get("/predictions")
async def v1_list_predictions(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
    source: Optional[str] = None,
    resolved: Optional[int] = None,
):
    """Paginated predictions with optional filters."""
    _validate_key(request)
    limit = max(1, min(limit, 500))

    where = []
    params: list = []
    if category:
        where.append("p.category = ?")
        params.append(category)
    if source:
        where.append("p.source_handle = ?")
        params.append(source)
    if resolved is not None:
        where.append("p.resolved = ?")
        params.append(resolved)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with db.conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM predictions p{where_sql}",
            tuple(params),
        ).fetchone()["n"]

        rows = c.execute(
            f"SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
            f"FROM predictions p "
            f"LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            f"{where_sql} ORDER BY p.extracted_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        ).fetchall()

    return JSONResponse({
        "predictions": [
            {
                "id": r["id"],
                "source_handle": r["source_handle"],
                "market_id": r["market_id"],
                "category": r["category"],
                "direction": r["direction"],
                "predicted_probability": r["predicted_probability"],
                "content": r["content"][:500],
                "extracted_at": r["extracted_at"],
                "resolved": bool(r["resolved"]),
                "resolved_correct": r["resolved_correct"],
                "source_credibility": r["global_credibility"],
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/markets/edge")
async def v1_markets_edge(
    request: Request,
    limit: int = 20,
    min_sources: int = 1,
    category: Optional[str] = None,
):
    """Top edge markets — where credibility intelligence disagrees most with price."""
    _validate_key(request)
    limit = max(1, min(limit, 50))

    import os
    from backend.markets import unified_markets
    from backend.markets.polymarket_client import PolymarketClient
    from backend.markets.kalshi_client import KalshiClient

    poly = PolymarketClient()
    kalshi = KalshiClient(
        base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
    )
    markets = await unified_markets.fetch_unified_markets(poly, kalshi, cache_ttl=300)
    await poly.close()
    await kalshi.close()

    active = [m for m in markets if m.status == "active"]
    enriched = unified_markets.enrich_markets_with_intelligence(active)
    with_edge = [
        m for m in enriched
        if m.betyc_ev_score is not None and m.betyc_prediction_count >= min_sources
    ]
    if category:
        with_edge = [m for m in with_edge if m.category == category]
    with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)

    return JSONResponse({
        "markets": [m.to_dict() for m in with_edge[:limit]],
        "total": len(with_edge),
    })

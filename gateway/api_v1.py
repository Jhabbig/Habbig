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
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import db
# TTL cache wrappers for hot read paths. Keys follow the canonical schema in
# gateway/cache/ttl.py; writes elsewhere in the app invalidate via
# ttl_invalidate.on_* helpers so callers don't see stale rows after a
# credibility recompute or a new prediction landing.
from cache import ttl_cache, DEFAULT_TTLS
from security.rate_limiter import get_client_ip, is_rate_limited as _is_rate_limited

log = logging.getLogger("api.v1")

router = APIRouter(prefix="/api/v1", tags=["v1"])

# ── Tier / scope policy ─────────────────────────────────────────────────────
#
# Free-tier keys (``tier='free'``) get read-only access to credibility +
# prediction data. The unified-markets edge endpoint hits Polymarket and
# Kalshi inline (see MED-4 in audits/audit_api_v1.md) and is gated to paid
# tiers only. ``standard`` and ``enterprise`` are paid; ``free`` is not.
_PAID_TIERS = frozenset({"standard", "enterprise"})

# Pre-auth IP rate limit: 30 requests per 60s per source IP. Fires BEFORE
# the DB lookup so an attacker probing random bearer tokens cannot burn
# DB round-trips at line speed. See HIGH-2 in audits/audit_api_v1.md.
_ANON_RATE_LIMIT = 30
_ANON_RATE_WINDOW = 60

# Bearer token length cap. Real narve keys are 'narve_' + token_urlsafe(32)
# ≈ 49 chars. We accept up to 256 to leave headroom for future key formats
# while keeping the SHA-256 work bounded. See HIGH-1.
_MAX_BEARER_LENGTH = 256


# ── API key helpers ─────────────────────────────────────────────────────────


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(user_id: int, name: str = "", tier: str = "standard") -> tuple[str, int]:
    """Create a new API key. Returns (raw_key, key_id).

    The raw key is shown ONCE. Only the hash is stored. We stamp
    ``first_displayed_at`` synchronously with creation so the key can
    never be retrieved a second time — any GET handler that returns
    raw key material MUST refuse when this column is non-null (M16).

    Migration 196 (``gateway/migrations/196_api_keys_first_displayed.py``)
    adds the column. The narrow fallback below exists ONLY to keep
    legacy pre-migration databases bootable; it catches the specific
    "no such column" OperationalError and lets every other SQL error
    propagate. See audits/audit_api_v1.md CRIT-1.
    """
    raw_key = f"narve_{secrets.token_urlsafe(32)}"
    key_hash = _hash_key(raw_key)
    prefix = raw_key[:12]
    rate_limit = 10000 if tier == "enterprise" else 1000
    now = int(time.time())

    with db.conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, user_id, name, tier, rate_limit_hour, created_at, first_displayed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key_hash, prefix, user_id, name, tier, rate_limit, now, now),
            )
        except sqlite3.OperationalError as exc:
            # Narrow fallback: only fire when the column itself is
            # missing on a legacy DB. Any other OperationalError
            # (UNIQUE violation, locked DB, malformed row) propagates
            # so the caller sees real failures. Once every deploy is
            # past migration 196 this branch can be deleted entirely.
            msg = str(exc).lower()
            if "no such column" not in msg or "first_displayed_at" not in msg:
                raise
            log.debug(
                "api_keys.first_displayed_at column missing; legacy insert (migration 196 not yet applied)"
            )
            cur = c.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, user_id, name, tier, rate_limit_hour, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key_hash, prefix, user_id, name, tier, rate_limit, now),
            )
        key_id = cur.lastrowid

    return raw_key, key_id


def get_api_key_raw(key_id: int, user_id: int) -> Optional[str]:
    """Return the raw key ONLY if it has never been displayed.

    SECURITY (M16): this function is the only sanctioned read path for
    raw key material. Any ``GET /api/keys/{id}`` endpoint MUST call
    this helper and 410 Gone when it returns None — the key hash
    stored in the DB is deliberately irreversible so this function
    cannot reconstruct a key after first display. It exists as a
    centralised guard: the create-and-hand-back flow in
    ``create_api_key`` has already stamped ``first_displayed_at``, so
    this always returns None in a correctly-migrated DB. The helper is
    provided so GET handlers have a single chokepoint to refuse
    re-display (and a single TODO to track when the column lands).
    """
    with db.conn() as c:
        try:
            row = c.execute(
                "SELECT first_displayed_at FROM api_keys WHERE id = ? AND user_id = ?",
                (key_id, user_id),
            ).fetchone()
        except Exception:
            return None
    if not row:
        return None
    # Already displayed → refuse. We never stored the plaintext, so
    # there is nothing to hand back regardless.
    return None


def _validate_key(request: Request) -> dict:
    """Validate the Bearer token. Returns the api_key row dict.

    Order of checks (audits/audit_api_v1.md HIGH-1/HIGH-2):
      1. Pre-auth per-IP rate limit (BEFORE any DB work) — caps the
         cost of guessed-bearer probes.
      2. Bearer presence + length cap — refuse oversized headers
         before hashing.
      3. SHA-256 lookup + revoked check.
      4. Per-key rate limit (tier-aware).
      5. ``last_used_at`` UPDATE.

    Raises HTTPException on failure.
    """
    # 1. Pre-auth IP rate limit — fires BEFORE the DB lookup so
    # anonymous traffic with random bearer tokens cannot burn a SQL
    # round-trip + SHA-256 per request.
    ip = get_client_ip(request)
    if _is_rate_limited(f"apiv1_anon:{ip}", _ANON_RATE_LIMIT, _ANON_RATE_WINDOW):
        raise HTTPException(
            429,
            "Too many requests from this IP. Slow down and try again.",
            headers={"Retry-After": str(_ANON_RATE_WINDOW)},
        )

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "API key required. Use: Authorization: Bearer <key>")

    # 2. Length cap — refuse oversized bearer tokens before we hash.
    # A real narve key is ~49 chars; 256 is generous for future formats
    # and bounds the SHA-256 work on the negative path.
    if len(auth) > _MAX_BEARER_LENGTH:
        raise HTTPException(401, "Invalid API key")

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

    # 4. Per-key rate limit
    from security.rate_limiter import limiter
    allowed, remaining, retry_after = limiter.check(
        f"apiv1:{row['id']}", row["rate_limit_hour"], 3600
    )
    if not allowed:
        raise HTTPException(
            429, "Rate limit exceeded",
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(row["rate_limit_hour"])},
        )

    # 5. Touch last_used_at
    with db.conn() as c:
        c.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (int(time.time()), row["id"]),
        )

    return dict(row)


def _key_tier(key: dict) -> str:
    """Lowercased tier string; defaults to 'free' if absent."""
    return (key.get("tier") or "free").strip().lower()


def _key_scopes(key: dict) -> set[str]:
    """Comma-separated scopes column → set. Defaults to {'read'} (M128)."""
    raw = key.get("scopes")
    if not raw:
        return {"read"}
    return {s.strip() for s in str(raw).split(",") if s.strip()}


def _require_scope(key: dict, scope: str) -> None:
    """403 if the API key does not have the named scope."""
    if scope not in _key_scopes(key):
        raise HTTPException(403, f"API key missing required scope: {scope}")


def _require_paid_tier(key: dict) -> None:
    """403 free-tier keys away from paid endpoints (audit HIGH-4).

    Free-tier keys can still read sources / predictions / single-market
    consensus. The unified-markets edge endpoint is paid-only because
    every call instantiates Polymarket + Kalshi clients and burns
    upstream rate budget — exactly the surface a free-tier abuser
    would target. See audits/audit_api_v1.md HIGH-4 and MED-4.
    """
    if _key_tier(key) not in _PAID_TIERS:
        raise HTTPException(
            403,
            "This endpoint requires a paid API key. Upgrade your plan to access /markets/edge.",
        )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/sources")
async def v1_list_sources(request: Request, limit: int = 100, offset: int = 0):
    """All sources with credibility scores.

    With no filter params → cached 120s keyed by page, flushed on
    credibility recompute. With saved-views filter params
    (``min_credibility``, ``max_credibility``, ``min_predictions``,
    ``categories_active``, ``handles``) → bypasses the cache and runs
    SQL directly against ``source_credibility`` (optionally joined to
    ``source_category_credibility``). Malformed filters drop silently.
    """
    key = _validate_key(request)
    _require_scope(key, "read")
    limit = max(1, min(limit, 500))

    try:
        import saved_views_schema as _sv
        sv_filters = _sv.filters_from_query("sources", request.query_params)
    except Exception:  # pragma: no cover
        sv_filters = {}

    # Fast path — no filters, cached pagination.
    if not sv_filters:
        page_num = offset // limit if limit else 0
        cache_key = f"sources:sort_default:filter_none:page_{page_num}_size_{limit}"

        def _compute() -> dict:
            sources = db.list_all_source_credibilities()
            page = sources[offset:offset + limit]
            return {
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
            }

        payload = ttl_cache.get_or_compute(cache_key, _compute, DEFAULT_TTLS["sources"])
        return JSONResponse(payload)

    # Filtered path — uncached, direct SQL.
    try:
        extra_where, extra_params, extra_joins, _ = _sv.build_where(
            "sources", sv_filters,
        )
    except Exception:
        extra_where, extra_params, extra_joins = "", [], []
    join_sql = " ".join(extra_joins) if extra_joins else ""
    # When a category join kicks in, the row set can duplicate per (source,
    # category); DISTINCT collapses it before pagination.
    distinct = "DISTINCT" if any("scc" in j for j in extra_joins) else ""
    where_clause = (" WHERE 1=1 " + extra_where) if extra_where else ""

    with db.conn() as c:
        total_row = c.execute(
            f"SELECT COUNT({distinct or '*'}{' sc.source_handle' if distinct else ''}) AS n "
            f"FROM source_credibility sc {join_sql}{where_clause}",
            tuple(extra_params),
        ).fetchone()
        rows = c.execute(
            f"SELECT {distinct} sc.* FROM source_credibility sc {join_sql}{where_clause} "
            f"ORDER BY sc.global_credibility DESC LIMIT ? OFFSET ?",
            tuple(extra_params) + (limit, offset),
        ).fetchall()

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
            for s in rows
        ],
        "total": total_row["n"] if total_row else 0,
        "limit": limit,
        "offset": offset,
        "filters_applied": sv_filters,
    })


@router.get("/sources/{handle}")
async def v1_source_detail(request: Request, handle: str):
    """Full source profile with calibration data. Cached 300s per handle;
    invalidated on new prediction or credibility recompute."""
    key = _validate_key(request)
    _require_scope(key, "read")

    def _compute() -> Optional[dict]:
        cred = db.get_source_credibility(handle)
        if not cred:
            return None
        cats = db.get_all_category_credibilities(handle)
        snaps = db.get_credibility_snapshots(handle, 10)
        cal = db.get_source_calibration(handle)
        return {
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
            "snapshots": [
                {"credibility": s["global_credibility"], "at": s["snapshot_at"]}
                for s in snaps
            ],
            "calibration": cal,
        }

    payload = ttl_cache.get_or_compute(
        f"source:{handle}", _compute, DEFAULT_TTLS["source"],
    )
    if payload is None:
        raise HTTPException(404, "Source not found")
    return JSONResponse(payload)


@router.get("/predictions")
async def v1_list_predictions(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
    source: Optional[str] = None,
    resolved: Optional[int] = None,
):
    """Paginated predictions with optional filters.

    Accepts the legacy ``category`` / ``source`` / ``resolved`` query params
    (left in place for API consumers) AND the full saved-views filter set
    (``categories``, ``sources``, ``posted_within``, ``resolution``,
    ``source_cred_range``) parsed via saved_views_schema.filters_from_query.
    When both the legacy single-value param and the plural saved-views
    param are present, the saved-views param wins — simpler semantics than
    merging two worlds. Malformed filter values are dropped, never 500.
    """
    key = _validate_key(request)
    _require_scope(key, "read")
    limit = max(1, min(limit, 500))

    # Legacy single-value params first — backwards compat.
    where = []
    params: list = []
    if category and "categories" not in request.query_params:
        where.append("p.category = ?")
        params.append(category)
    if source and "sources" not in request.query_params:
        where.append("p.source_handle = ?")
        params.append(source)
    if resolved is not None and "resolution" not in request.query_params:
        where.append("p.resolved = ?")
        params.append(resolved)

    # Saved-views filter schema — full filter set, scope=predictions.
    try:
        import saved_views_schema as _sv
        sv_filters = _sv.filters_from_query("predictions", request.query_params)
        extra_where, extra_params, extra_joins, _ = _sv.build_where(
            "predictions", sv_filters,
        )
    except Exception:  # pragma: no cover — defensive, never 500
        sv_filters, extra_where, extra_params, extra_joins = {}, "", [], []

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    if extra_where:
        # extra_where starts with "AND ..." — splice correctly whether or not
        # we already have a WHERE clause.
        if where_sql:
            where_sql += " " + extra_where
        else:
            where_sql = " WHERE 1=1 " + extra_where
    extra_joins_sql = " ".join(extra_joins) if extra_joins else ""

    with db.conn() as c:
        total = c.execute(
            f"SELECT COUNT(*) AS n FROM predictions p "
            f"LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            f"{extra_joins_sql}{where_sql}",
            tuple(params) + tuple(extra_params),
        ).fetchone()["n"]

        rows = c.execute(
            f"SELECT p.*, sc.global_credibility, sc.accuracy_unlocked "
            f"FROM predictions p "
            f"LEFT JOIN source_credibility sc ON sc.source_handle = p.source_handle "
            f"{extra_joins_sql}{where_sql} ORDER BY p.extracted_at DESC LIMIT ? OFFSET ?",
            tuple(params) + tuple(extra_params) + (limit, offset),
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
        "filters_applied": sv_filters,  # Echoed back for client sanity.
    })


@router.get("/markets/{slug:path}/consensus")
async def v1_market_consensus(request: Request, slug: str):
    """Credibility-weighted consensus probability for a single market.

    Wraps ``queries.predictions.calculate_betyc_probability`` and caches the
    response at ``credibility_consensus:{slug}`` with a 60s TTL. The
    invalidation facade (``ttl_invalidate.on_new_prediction`` and
    ``on_market_resolved``) already drops this key when predictions land or a
    market settles, so the TTL is mostly a back-stop.

    Response shape:

        {
          "slug": str,
          "betyc_yes_probability": float | None,  # [0.05, 0.95] or null
          "betyc_no_probability":  float | None,
          "betyc_edge":            float | None,
          "betyc_source_count":    int,
          "betyc_confidence":      str,
          "avg_source_credibility": float | None,
          "cached_for_seconds":     int,
        }

    Returns 404 when no predictions exist for the slug (rather than an empty
    consensus object) so clients can distinguish "no data" from "zero edge".
    """
    key = _validate_key(request)
    _require_scope(key, "read")

    def _compute() -> Optional[dict]:
        preds = db.get_predictions_for_market(slug)
        if not preds:
            return None
        pred_dicts = [
            {
                "source_handle": p["source_handle"],
                "direction": p["direction"],
                "predicted_probability": p["predicted_probability"],
                "global_credibility": p["global_credibility"],
                "category_credibility": (
                    p["category_credibility"]
                    if "category_credibility" in p.keys() else None
                ),
                "accuracy_unlocked": (
                    bool(p["accuracy_unlocked"])
                    if p["accuracy_unlocked"] is not None else False
                ),
            }
            for p in preds
        ]
        result = db.calculate_betyc_probability(pred_dicts)
        avg_cred = (
            sum((d.get("global_credibility") or 0.5) for d in pred_dicts)
            / max(len(pred_dicts), 1)
        )
        return {
            "slug": slug,
            "betyc_yes_probability": result.get("betyc_yes_probability"),
            "betyc_no_probability": result.get("betyc_no_probability"),
            "betyc_edge": result.get("betyc_edge"),
            "betyc_source_count": result.get("betyc_source_count", 0),
            "betyc_confidence": result.get("betyc_confidence", "Insufficient data"),
            "avg_source_credibility": round(avg_cred, 4),
            "cached_for_seconds": DEFAULT_TTLS["credibility_consensus"],
        }

    payload = ttl_cache.get_or_compute(
        f"credibility_consensus:{slug}",
        _compute,
        DEFAULT_TTLS["credibility_consensus"],
    )
    if payload is None:
        raise HTTPException(404, "No predictions exist for this market")
    return JSONResponse(payload)


@router.get("/markets/edge")
async def v1_markets_edge(
    request: Request,
    limit: int = 20,
    min_sources: int = 1,
    category: Optional[str] = None,
):
    """Top edge markets — where credibility intelligence disagrees most with price.

    Paid-tier only: this endpoint instantiates Polymarket + Kalshi
    clients on every request (see MED-4) and burns upstream rate
    budget that free-tier abuse would exhaust. Free-tier keys get
    a 403 here; standard / enterprise pass through.
    """
    key = _validate_key(request)
    _require_scope(key, "read")
    _require_paid_tier(key)
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

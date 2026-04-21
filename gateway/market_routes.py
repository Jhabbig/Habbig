"""Markets routes — unified Polymarket + Kalshi API, trading, portfolio, Kelly.

Extracted from server.py. Every route in this module was previously a
top-level ``@app.*`` decorator in server.py. Zero behaviour change — the
handlers below are byte-identical copies of the originals except that all
cross-module references go through ``_srv()`` so imports stay one-way.

Module-level singletons that other server.py sites still reference
(``POLY_CLIENT``, ``KALSHI_CLIENT``, ``MARKETS_CACHE_TTL``,
``_get_market_connections``) stay in server.py. The per-block constants
that only the route handlers care about (``POLY_EXCHANGE_ADDRESS``,
``POLY_NEG_RISK_EXCHANGE_ADDRESS``, the domain/version literals) moved
here alongside the handlers that use them.
"""

from __future__ import annotations

import logging
import sys
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

import db
from backend.markets import unified_markets
from backend.markets.encryption import encrypt_token, decrypt_token
from backend.markets.portfolio_aggregator import get_combined_orders
from cache import ttl_cache, DEFAULT_TTLS


log = logging.getLogger("gateway.market_routes")


def _srv():
    """Return the already-imported server module for shared helpers/globals."""
    return sys.modules.get("server") or sys.modules["__main__"]


# Polymarket CTF Exchange contract (Polygon mainnet).
# https://github.com/Polymarket/ctf-exchange
POLY_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLY_NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
POLY_CHAIN_ID = 137
POLY_DOMAIN_NAME = "Polymarket CTF Exchange"
POLY_DOMAIN_VERSION = "1"


def _require_markets_user(request: Request) -> dict:
    """Require authenticated user with active Trading Add-on for markets access."""
    srv = _srv()
    user = srv.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    # Admin bypasses all checks
    if user.get("is_admin"):
        return user
    # Require trading add-on (separate from base subscription)
    if not db.has_trading_addon(user["user_id"]):
        raise HTTPException(status_code=403, detail="Trading Add-on required. Contact us to add trading access.")
    return user


async def _build_enriched_portfolio(user_id: int) -> dict:
    """Thin wrapper — routes and background jobs both go through
    portfolio_sync so there's one implementation of signal enrichment,
    persistence, and Kalshi-401 deactivation."""
    from backend.markets.portfolio_sync import sync_user_portfolio
    srv = _srv()
    return await sync_user_portfolio(
        user_id,
        poly_client=srv.POLY_CLIENT,
        kalshi_client=srv.KALSHI_CLIENT,
        unified_markets_module=unified_markets,
        markets_cache_ttl=srv.MARKETS_CACHE_TTL,
    )


# ── Route handlers ───────────────────────────────────────────────────────────


async def api_markets_unified(
    request: Request,
    category: str = "",
    search: str = "",
    sort: str = "volume",
    source: str = "",
    page: int = 1,
    limit: int = 20,
    env_relevant: int = 0,
):
    srv = _srv()
    user = _require_markets_user(request)  # auth + add-on check
    # Clamp pagination params to prevent division by zero and negative indexing
    if limit < 1 or limit > 100:
        limit = 20
    if page < 1:
        page = 1
    markets = await unified_markets.fetch_unified_markets(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, cache_ttl=srv.MARKETS_CACHE_TTL,
    )
    # Only the filtered-sorted slice is cached (30s TTL). `search` is user-
    # typed free text that would blow up the keyspace, so skip cache when
    # set. env_relevant also changes the dataset per-user (Pro feature) —
    # skip then too.
    cacheable = not search and not env_relevant
    filter_cache_key = (
        f"markets:cat_{category or 'all'}:sort_{sort}"
        f":src_{source or 'all'}:page_{page}:lim_{limit}"
    )
    if cacheable:
        filtered = ttl_cache.get_or_compute(
            filter_cache_key,
            lambda: unified_markets.filter_markets(
                markets, category=category, source=source, search=search, sort=sort,
            ),
            DEFAULT_TTLS["markets"],
        )
    else:
        filtered = unified_markets.filter_markets(
            markets, category=category, source=source, search=search, sort=sort,
        )
    # env_relevant filter — only return markets that have a cached env analysis
    # marked is_relevant=True. Reads from the cache only; never triggers Claude
    # generation during list pagination.
    env_relevant_ids: set[str] = set()
    if env_relevant:
        try:
            top = db.list_top_environmental_impacts(limit=200)
            env_relevant_ids = {row["market_id"] for row in top}
        except Exception as exc:
            log.warning("env_relevant filter failed, returning unfiltered: %s", exc)
            env_relevant_ids = set()
        if env_relevant_ids:
            filtered = [m for m in filtered if m.id in env_relevant_ids]
    total = len(filtered)
    start = (page - 1) * limit
    page_markets = filtered[start:start + limit]
    market_dicts = [m.to_dict() for m in page_markets]
    # When env_relevant filter is active, decorate each row with a small
    # is_env_relevant flag so downstream UIs can render a leaf badge without
    # a second roundtrip per market.
    if env_relevant_ids:
        for md in market_dicts:
            md["is_env_relevant"] = md.get("id") in env_relevant_ids
    payload = {
        "markets": market_dicts,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }
    return JSONResponse(srv._forensic_sign(user, payload, "api_markets_unified"))


async def api_markets_top_edge(
    request: Request,
    limit: int = 20,
    min_sources: int = 1,
    category: str = "",
):
    """Markets with the largest absolute edge between credibility-weighted
    intelligence and the current market price. The core value proposition
    of narve.ai — "where is the crowd most wrong?"
    """
    srv = _srv()
    user = srv._require_authenticated(request)
    limit = max(1, min(50, limit))
    markets = await unified_markets.fetch_unified_markets(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, cache_ttl=srv.MARKETS_CACHE_TTL,
    )

    # Best-bets / top-edge ranking is tier-gated (higher tiers see more
    # rows; admin sees everything). Key on the effective tier so a Pro and
    # a Free user don't share the same list.
    tier = "admin" if user.get("is_admin") else (user.get("plan") or "free")
    cache_key = (
        f"best_bets:tier_{tier}:page_1"
        f":lim_{limit}:min_{min_sources}:cat_{category or 'all'}"
    )

    def _compute() -> dict:
        active = [m for m in markets if m.status == "active"]
        enriched = unified_markets.enrich_markets_with_intelligence(active)
        with_edge = [
            m for m in enriched
            if m.betyc_ev_score is not None and m.betyc_prediction_count >= min_sources
        ]
        if category:
            with_edge = [m for m in with_edge if m.category == category]
        with_edge.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
        return {
            "markets": [m.to_dict() for m in with_edge[:limit]],
            "total": len(with_edge),
        }

    payload = ttl_cache.get_or_compute(cache_key, _compute, DEFAULT_TTLS["best_bets"])
    return JSONResponse(srv._forensic_sign(user, dict(payload), "api_markets_top_edge"))


async def api_markets_false_consensus(request: Request, limit: int = 20):
    """Markets where a high market price (>80% or <20%) disagrees strongly
    with credibility-weighted intelligence (divergence > 15 points).
    These are the highest-conviction contrarian bets.
    """
    srv = _srv()
    user = srv._require_authenticated(request)
    limit = max(1, min(50, limit))
    markets = await unified_markets.fetch_unified_markets(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, cache_ttl=srv.MARKETS_CACHE_TTL,
    )
    active = [m for m in markets if m.status == "active"]
    enriched = unified_markets.enrich_markets_with_intelligence(active)
    fc_markets = [m for m in enriched if m.false_consensus]
    fc_markets.sort(key=lambda m: abs(m.betyc_ev_score or 0), reverse=True)
    payload = {
        "markets": [m.to_dict() for m in fc_markets[:limit]],
        "total": len(fc_markets),
    }
    return JSONResponse(srv._forensic_sign(user, payload, "api_markets_false_consensus"))


async def api_market_detail(request: Request, market_id: str):
    srv = _srv()
    user = _require_markets_user(request)
    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    try:
        from engagement import log_event
        log_event(user["user_id"], "market_detail_view", metadata={"market_id": market_id})
    except Exception:
        pass
    # Cache the base dict shape (no env overlay) so repeated views hit the
    # fast path. Pro env overlay + forensic sign still run per-request.
    payload = ttl_cache.get_or_compute(
        f"market:{market_id}",
        lambda: market.to_dict(),
        DEFAULT_TTLS["market"],
    )
    # Returned dict is cached — clone before mutating for per-user overlay.
    payload = dict(payload)
    # If the caller is Pro+ AND has env preferences enabled AND a cached
    # env analysis exists, merge it into the response under environmental_impact.
    # This is non-breaking: clients that don't know about the field ignore it,
    # and we never block on Claude generation here — only return cached data.
    try:
        is_pro = bool(user.get("is_admin"))
        if not is_pro:
            _subs = {s["dashboard_key"]: s for s in db.list_subscriptions(user["user_id"])}
            _pinfo = srv._user_plan_info(user, _subs, int(time.time()))
            is_pro = _pinfo.get("plan") == "pro"
        if is_pro:
            prefs = db.get_user_env_preferences(user["user_id"])
            if prefs.get("show"):
                cached = db.get_environmental_impact(market_id)
                if cached:
                    from intelligence import environmental as _env
                    env_payload = _env._row_to_payload(cached)
                    env_payload = _env.apply_user_unit_preference(env_payload, prefs.get("unit", "co2_mt"))
                    payload["environmental_impact"] = env_payload
    except Exception as exc:
        log.warning("env merge into market detail failed for %s: %s", market_id, exc)
    return JSONResponse(payload)


async def api_markets_search(request: Request, q: str = ""):
    srv = _srv()
    user = _require_markets_user(request)  # auth + add-on check
    if not q or len(q) < 2:
        return JSONResponse({"markets": []})
    markets = await unified_markets.fetch_unified_markets(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, cache_ttl=srv.MARKETS_CACHE_TTL,
    )
    filtered = unified_markets.filter_markets(markets, search=q)
    return JSONResponse(srv._forensic_sign(
        user, {"markets": [m.to_dict() for m in filtered[:20]]}, "api_markets_search",
    ))


async def api_connect_kalshi(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    email = (body.get("email") or "").strip()
    password = body.get("password", "")
    if not email or not password:
        return JSONResponse({"error": "Email and password are required"}, status_code=400)

    result = await srv.KALSHI_CLIENT.login(email, password)
    if "error" in result:
        status_code = result.get("status_code", 400)
        return JSONResponse({"error": result["error"]}, status_code=status_code)

    # Store encrypted token — NEVER store the password
    encrypted = encrypt_token(result["token"])
    db.upsert_market_credential(
        user["user_id"], "kalshi",
        kalshi_token=encrypted,
        kalshi_member_id=result["member_id"],
    )
    log.info("User %s connected Kalshi account (member: %s)", user.get("username"), result["member_id"])

    # Fetch balance
    balance_data = await srv.KALSHI_CLIENT.get_balance(result["token"])
    balance = float(balance_data.get("balance", 0)) / 100.0 if "error" not in balance_data else None

    return JSONResponse({
        "connected": True,
        "member_id": result["member_id"],
        "balance": balance,
    })


async def api_connect_polymarket(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    address = (body.get("wallet_address") or "").strip()
    if not address or len(address) < 10:
        return JSONResponse({"error": "Valid wallet address required"}, status_code=400)
    db.upsert_market_credential(
        user["user_id"], "polymarket",
        polymarket_wallet_address=address,
    )
    log.info("User %s connected Polymarket wallet %s", user.get("username"), address[:10] + "...")
    return JSONResponse({"connected": True, "address": address})


async def api_disconnect_market(request: Request, source: str):
    user = _require_markets_user(request)
    if source not in ("polymarket", "kalshi"):
        raise HTTPException(status_code=400, detail="Invalid source")
    db.disconnect_market_credential(user["user_id"], source)
    db.delete_user_positions(user["user_id"], platform=source)
    log.info("User %s disconnected %s", user.get("username"), source)
    return JSONResponse({"disconnected": True})


async def api_market_connections(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    return JSONResponse(srv._forensic_sign(
        user, srv._get_market_connections(user["user_id"]), "api_market_connections",
    ))


async def api_bet_kalshi(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    ticker = (body.get("ticker") or "").strip()
    side = (body.get("side") or "").strip().lower()
    amount_usd = float(body.get("amount_usd", 0))
    order_type = (body.get("type") or "market").strip().lower()
    price = body.get("price")

    if not ticker:
        return JSONResponse({"error": "Ticker required"}, status_code=400)
    if side not in ("yes", "no"):
        return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
    if amount_usd <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    if amount_usd > 25000:
        return JSONResponse({"error": "Amount exceeds maximum"}, status_code=400)

    cred = db.get_market_credential(user["user_id"], "kalshi")
    if not cred or not cred["kalshi_token"]:
        return JSONResponse({"error": "Connect your Kalshi account first"}, status_code=400)

    token = decrypt_token(cred["kalshi_token"])
    db.update_market_credential_last_used(user["user_id"], "kalshi")

    # Validate balance
    balance_data = await srv.KALSHI_CLIENT.get_balance(token)
    if "error" in balance_data:
        if balance_data.get("error") == "token_expired":
            db.set_market_credential_active(user["user_id"], "kalshi", False)
            return JSONResponse({"error": "Kalshi session expired — please reconnect"}, status_code=401)
        return JSONResponse({"error": balance_data["error"]}, status_code=400)

    balance_cents = balance_data.get("balance", 0)
    if amount_usd * 100 > balance_cents:
        return JSONResponse({"error": f"Insufficient balance (${balance_cents / 100:.2f} available)"}, status_code=400)

    count = max(1, int(amount_usd))  # Kalshi uses contract counts
    # Coerce price to float — client may send int, float, or numeric string.
    # Clamp to Kalshi's valid range (1-99 cents) and reject garbage.
    price_cents = None
    if order_type == "limit" and price is not None:
        try:
            price_float = float(price)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid limit price"}, status_code=400)
        if not (0 < price_float < 1):
            return JSONResponse({"error": "Limit price must be between 0 and 1"}, status_code=400)
        price_cents = max(1, min(99, int(round(price_float * 100))))

    result = await srv.KALSHI_CLIENT.place_order(
        token,
        ticker=ticker,
        side=side,
        order_type=order_type,
        count=count,
        price=price_cents,
    )

    if "error" in result:
        if result.get("error") == "token_expired":
            db.set_market_credential_active(user["user_id"], "kalshi", False)
            return JSONResponse({"error": "Kalshi session expired — please reconnect"}, status_code=401)
        return JSONResponse({"error": result["error"]}, status_code=400)

    # Record in history
    db.record_bet(
        user["user_id"], "kalshi", result.get("order_id", ""),
        f"kalshi:{ticker}", ticker, side, amount_usd,
        price or 0, result.get("status", "submitted"),
    )

    return JSONResponse({
        "order_id": result.get("order_id", ""),
        "status": result.get("status", "submitted"),
        "filled": result.get("filled", 0),
    })


async def api_bet_polymarket(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    market_id = (body.get("market_id") or "").strip()
    side = (body.get("side") or "").strip().lower()
    amount_usdc = float(body.get("amount_usdc", 0))
    signed_order = body.get("signed_order")
    owner = (body.get("owner") or "").strip()

    if not market_id:
        return JSONResponse({"error": "Market ID required"}, status_code=400)
    if side not in ("yes", "no"):
        return JSONResponse({"error": "Side must be 'yes' or 'no'"}, status_code=400)
    if amount_usdc <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)
    if amount_usdc > 100000:
        return JSONResponse({"error": "Amount exceeds maximum"}, status_code=400)
    if not signed_order or not isinstance(signed_order, dict):
        return JSONResponse({"error": "Signed order required (sign with your wallet)"}, status_code=400)

    # Validate signed_order structure — must include all CTF Exchange Order fields
    required_fields = {
        "salt", "maker", "signer", "taker", "tokenId",
        "makerAmount", "takerAmount", "expiration", "nonce",
        "feeRateBps", "side", "signatureType", "signature",
    }
    missing = required_fields - set(signed_order.keys())
    if missing:
        return JSONResponse(
            {"error": f"Signed order missing fields: {', '.join(sorted(missing))}"},
            status_code=400,
        )

    cred = db.get_market_credential(user["user_id"], "polymarket")
    if not cred or not cred["polymarket_wallet_address"]:
        return JSONResponse({"error": "Connect your Polymarket wallet first"}, status_code=400)

    # Security: signer/maker MUST match the connected wallet — prevents user A
    # from submitting orders signed by user B's wallet.
    connected_addr = (cred["polymarket_wallet_address"] or "").lower()
    signer_addr = str(signed_order.get("signer", "")).lower()
    maker_addr = str(signed_order.get("maker", "")).lower()
    if signer_addr != connected_addr or maker_addr != connected_addr:
        log.warning(
            "Polymarket bet rejected: signer/maker %s/%s does not match connected %s for user %s",
            signer_addr[:10], maker_addr[:10], connected_addr[:10], user.get("username"),
        )
        return JSONResponse(
            {"error": "Signed order wallet does not match your connected wallet"},
            status_code=403,
        )

    db.update_market_credential_last_used(user["user_id"], "polymarket")

    # Polymarket CLOB expects {order, owner, orderType} envelope
    clob_payload = {
        "order": signed_order,
        "owner": owner or connected_addr,
        "orderType": body.get("order_type", "GTC"),
    }

    result = await srv.POLY_CLIENT.submit_order(clob_payload)

    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=400)

    db.record_bet(
        user["user_id"], "polymarket", result.get("orderID", result.get("id", "")),
        market_id, market_id, side, amount_usdc, 0, "submitted",
    )

    return JSONResponse({
        "order_id": result.get("orderID", result.get("id", "")),
        "status": "submitted",
    })


async def api_poly_order_params(request: Request, market_id: str):
    """Return the EIP-712 order parameters the client needs to sign a Polymarket order.

    The client uses these to construct an EIP-712 typed data object and sign it
    with eth_signTypedData_v4 via MetaMask. The signed order is then POSTed
    to /api/markets/bet/polymarket for submission to the CLOB.
    """
    srv = _srv()
    user = _require_markets_user(request)

    if not market_id.startswith("poly:"):
        raise HTTPException(status_code=400, detail="Only Polymarket markets supported")

    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    if not market.poly_yes_token_id or not market.poly_no_token_id:
        raise HTTPException(
            status_code=400,
            detail="Market missing CLOB token IDs — cannot place orders on this market",
        )

    cred = db.get_market_credential(user["user_id"], "polymarket")
    if not cred or not cred["polymarket_wallet_address"]:
        raise HTTPException(status_code=400, detail="Connect your Polymarket wallet first")

    exchange = POLY_NEG_RISK_EXCHANGE_ADDRESS if market.poly_neg_risk else POLY_EXCHANGE_ADDRESS

    return JSONResponse({
        "market_id": market_id,
        "yes_token_id": market.poly_yes_token_id,
        "no_token_id": market.poly_no_token_id,
        "yes_price": market.yes_price,
        "no_price": market.no_price,
        "neg_risk": market.poly_neg_risk,
        "maker_address": cred["polymarket_wallet_address"],
        "exchange": exchange,
        "chain_id": POLY_CHAIN_ID,
        "domain_name": POLY_DOMAIN_NAME,
        "domain_version": POLY_DOMAIN_VERSION,
        "fee_rate_bps": 0,
    })


async def api_markets_portfolio(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    portfolio = await _build_enriched_portfolio(user["user_id"])
    return JSONResponse(srv._forensic_sign(user, portfolio, "api_markets_portfolio"))


async def api_markets_orders(request: Request):
    srv = _srv()
    user = _require_markets_user(request)
    creds = db.get_all_market_credentials(user["user_id"])

    poly_address = None
    kalshi_token = None
    for c in creds:
        if not c["is_active"]:
            continue
        if c["source"] == "polymarket":
            poly_address = c["polymarket_wallet_address"]
        elif c["source"] == "kalshi" and c["kalshi_token"]:
            kalshi_token = decrypt_token(c["kalshi_token"])

    orders = await get_combined_orders(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT,
        polymarket_address=poly_address,
        kalshi_token=kalshi_token,
    )
    return JSONResponse(srv._forensic_sign(user, {"orders": orders}, "api_markets_orders"))


async def api_markets_sync(request: Request):
    """Force-refresh positions from both exchanges. Rate-limited to 1/min
    per user so the refresh button can't hammer upstream APIs."""
    srv = _srv()
    user = _require_markets_user(request)
    if srv._is_rate_limited(f"portfolio_sync:{user['user_id']}", 1, 60):
        raise HTTPException(status_code=429, detail="Sync rate limit — try again in a moment")
    portfolio = await _build_enriched_portfolio(user["user_id"])
    return JSONResponse({
        "synced": True,
        "synced_at": int(time.time()),
        "combined_total_usd": portfolio.get("combined_total_usd", 0),
    })


async def api_markets_stats(request: Request):
    """Aggregate portfolio stats for the dashboard header cards."""
    user = _require_markets_user(request)
    stats = db.get_portfolio_stats(user["user_id"])
    return JSONResponse(stats)


async def api_kelly_calculate(request: Request):
    """Kelly sizing for a specific market.

    Body: { market_id: str, bankroll?: float }
    `market_id` is the unified id (poly:{slug} or kalshi:{ticker}).
    `bankroll` falls back to the user's stored bankroll; returns 400 if
    neither is available. Returns full / half / quarter Kelly so the UI
    can show all three tiers without three round-trips.
    """
    srv = _srv()
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    market_id = (body.get("market_id") or body.get("market_slug") or "").strip()
    if not market_id:
        return JSONResponse({"error": "market_id required"}, status_code=400)

    stored = db.get_user_bankroll(user["user_id"])
    req_bankroll = body.get("bankroll")
    bankroll = float(req_bankroll) if req_bankroll is not None else stored["bankroll"]
    if bankroll is None or bankroll <= 0:
        return JSONResponse(
            {"error": "Set your bankroll first — PATCH /api/user/bankroll"},
            status_code=400,
        )

    market = await unified_markets.fetch_single_market(
        srv.POLY_CLIENT, srv.KALSHI_CLIENT, market_id, cache_ttl=120,
    )
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    enriched = unified_markets.enrich_markets_with_intelligence([market])
    m = enriched[0] if enriched else market
    if m.betyc_ev_score is None:
        return JSONResponse({
            "market_id": market_id,
            "bankroll": bankroll,
            "has_signal": False,
            "market_yes_price": m.yes_price,
            "narve_yes_probability": None,
            "edge": 0,
            "recommendations": [],
            "message": "No narve.ai signal yet — need predictions before Kelly can size.",
        })

    narve_yes = max(0.0, min(1.0, m.yes_price + m.betyc_ev_score))
    recommendations = []
    for label, frac in (("full", 1.0), ("half", 0.5), ("quarter", 0.25)):
        sizing = unified_markets.compute_kelly_sizing(
            betyc_probability=narve_yes,
            market_yes_price=m.yes_price,
            bankroll=bankroll,
            fraction=frac,
        )
        bet = float(sizing.get("recommended_amount") or 0)
        price = m.yes_price if sizing.get("side") == "YES" else (1 - m.yes_price)
        max_profit = round(bet * ((1 / price) - 1), 2) if price > 0 else 0.0
        max_loss = round(bet, 2)
        recommendations.append({
            "label": label,
            "fraction_of_kelly": frac,
            "side": sizing.get("side"),
            "kelly_full_fraction": sizing.get("kelly_full_fraction"),
            "kelly_adjusted_fraction": sizing.get("kelly_adjusted_fraction"),
            "bet_amount_usd": bet,
            "pct_of_bankroll": round((bet / bankroll) * 100, 4) if bankroll > 0 else 0,
            "max_profit_usd": max_profit,
            "max_loss_usd": max_loss,
        })

    return JSONResponse({
        "market_id": market_id,
        "market_title": m.title,
        "market_yes_price": m.yes_price,
        "narve_yes_probability": round(narve_yes, 4),
        "edge": round(narve_yes - m.yes_price, 4),
        "bankroll": bankroll,
        "has_signal": True,
        "recommendations": recommendations,
    })


async def api_user_bankroll_get(request: Request):
    user = _require_markets_user(request)
    return JSONResponse(db.get_user_bankroll(user["user_id"]))


async def api_user_bankroll_set(request: Request):
    user = _require_markets_user(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)

    bankroll = body.get("bankroll")
    kelly_fraction = body.get("kelly_fraction")

    if bankroll is not None:
        try:
            bankroll = float(bankroll)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid bankroll"}, status_code=400)
        if bankroll < 0 or bankroll > 1_000_000_000:
            return JSONResponse(
                {"error": "Bankroll must be between 0 and 1,000,000,000"},
                status_code=400,
            )

    if kelly_fraction is not None:
        try:
            kelly_fraction = float(kelly_fraction)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid kelly_fraction"}, status_code=400)
        if not (0 < kelly_fraction <= 1):
            return JSONResponse(
                {"error": "kelly_fraction must be between 0 and 1"},
                status_code=400,
            )

    db.set_user_bankroll(user["user_id"], bankroll=bankroll, kelly_fraction=kelly_fraction)
    return JSONResponse(db.get_user_bankroll(user["user_id"]))


def register(app) -> None:
    """Wire markets + Kelly + bankroll routes into the given FastAPI app.

    Route ORDER matters: /api/markets/top-edge and /api/markets/false-consensus
    and /api/markets/search must be registered BEFORE /api/markets/unified/{market_id:path}
    (the catch-all) so FastAPI doesn't consume their names as path params.
    """
    app.add_api_route("/api/markets/unified", api_markets_unified, methods=["GET"])
    app.add_api_route("/api/markets/top-edge", api_markets_top_edge, methods=["GET"])
    app.add_api_route("/api/markets/false-consensus", api_markets_false_consensus, methods=["GET"])
    app.add_api_route("/api/markets/unified/{market_id:path}", api_market_detail, methods=["GET"])
    app.add_api_route("/api/markets/search", api_markets_search, methods=["GET"])
    app.add_api_route("/api/markets/connect/kalshi", api_connect_kalshi, methods=["POST"])
    app.add_api_route("/api/markets/connect/polymarket", api_connect_polymarket, methods=["POST"])
    app.add_api_route("/api/markets/connect/{source}", api_disconnect_market, methods=["DELETE"])
    app.add_api_route("/api/markets/connections", api_market_connections, methods=["GET"])
    app.add_api_route("/api/markets/bet/kalshi", api_bet_kalshi, methods=["POST"])
    app.add_api_route("/api/markets/bet/polymarket", api_bet_polymarket, methods=["POST"])
    app.add_api_route("/api/markets/poly/order-params/{market_id:path}", api_poly_order_params, methods=["GET"])
    app.add_api_route("/api/markets/portfolio", api_markets_portfolio, methods=["GET"])
    app.add_api_route("/api/markets/orders", api_markets_orders, methods=["GET"])
    app.add_api_route("/api/markets/sync", api_markets_sync, methods=["POST"])
    app.add_api_route("/api/markets/stats", api_markets_stats, methods=["GET"])
    app.add_api_route("/api/kelly/calculate", api_kelly_calculate, methods=["POST"])
    app.add_api_route("/api/user/bankroll", api_user_bankroll_get, methods=["GET"])
    app.add_api_route("/api/user/bankroll", api_user_bankroll_set, methods=["PATCH"])

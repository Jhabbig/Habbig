#!/usr/bin/env python3
"""Crypto Trackers Dashboard - FastAPI backend.

Best-in-class crypto data infrastructure: universe browser, multi-exchange
price/depth/funding, cross-exchange arbitrage scanner, DeFi TVL, stablecoin
view, Fear & Greed index, per-source health monitor.

The product thesis: in the long run the most valuable features aren't the
neural-net predictions, they're the **data trackers** themselves - speed,
breadth, fidelity. This dashboard ships those, for every coin, with the
features every competing product has (CoinMarketCap, CoinGecko, TradingView,
DexScreener, DefiLlama, Whale Alert, Glassnode-lite).

Auth: same gateway-SSO pattern as the other narve dashboards. Set DEV_MODE=1
to bypass when running locally.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from analysis import arbitrage as arb_mod
from analysis import carry as carry_mod
from analysis import dca_simulator
from analysis import dex_cex_premium
from analysis import position_sizer
from analysis import funding as funding_mod
from analysis import liquidations_agg
from analysis import lp_il
from analysis import onchain_lookup
from analysis import screener as screener_mod
from analysis import sectors as sectors_mod
from analysis import tax_lots
from ingestion import (
    _background,
    _health,
    _persistence,
    alerts as alerts_mod,
    binance,
    defillama_bridges,
    defillama_fees,
    defillama_yields,
    nft_floors,
    pumpfun,
    rekt_hacks,
    stablecoin_peg,
    token_unlocks,
    binance_liquidations,
    btc_treasuries,
    bybit,
    bybit_liquidations,
    coinbase,
    coingecko,
    defillama,
    defillama_prices,
    deribit,
    etherscan_gas,
    etherscan_token,
    fear_greed,
    hyperliquid,
    jito,
    kraken,
    macro,
    mempool_btc,
    news,
    okx,
    okx_liquidations,
    portfolio,
    solana,
    solscan,
    whales,
    ws_broadcaster,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ct")

app = FastAPI(title="Crypto Trackers Dashboard")

HTML_PATH = Path(__file__).parent / "index.html"
COIN_HTML_PATH = Path(__file__).parent / "coin.html"
COMPARE_HTML_PATH = Path(__file__).parent / "compare.html"
MULTIPANE_HTML_PATH = Path(__file__).parent / "multipane.html"
PRICING_HTML_PATH = Path(__file__).parent / "pricing.html"
WELCOME_HTML_PATH = Path(__file__).parent / "welcome.html"
PORTFOLIO_HTML_PATH = Path(__file__).parent / "portfolio.html"
DIGEST_HTML_PATH = Path(__file__).parent / "digest.html"
CHANGELOG_HTML_PATH = Path(__file__).parent / "changelog.html"
STATUS_HTML_PATH = Path(__file__).parent / "status.html"
GUIDE_PUMP_PATH = Path(__file__).parent / "guide-pump-and-dump.html"
SETUPS_HTML_PATH = Path(__file__).parent / "setups.html"
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_sso_secret = os.environ.get("GATEWAY_SSO_SECRET", "")
_DEV_MODE = os.environ.get("DEV_MODE", "").strip() == "1"

# Optional API-key tier: when CT_API_KEYS is set (comma-separated list of
# bearer tokens), any /api/* request carrying Authorization: Bearer <token>
# matching one of them is accepted **without** requiring the gateway-SSO
# header. Useful for programmatic external consumers (quants, bots).
_api_keys: set[str] = set()
_api_keys_raw = os.environ.get("CT_API_KEYS", "").strip()
if _api_keys_raw:
    _api_keys = {k.strip() for k in _api_keys_raw.split(",") if k.strip()}
if not _sso_secret and not _DEV_MODE:
    log.warning("GATEWAY_SSO_SECRET unset and DEV_MODE off - all requests will 503")


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    if request.url.path != "/healthz":
        # API-key tier: bearer token in Authorization header. Checked first
        # so external consumers don't need to spoof the gateway secret.
        auth_header = request.headers.get("authorization", "")
        token_ok = False
        if _api_keys and auth_header.lower().startswith("bearer "):
            presented = auth_header.split(" ", 1)[1].strip()
            for key in _api_keys:
                if hmac.compare_digest(presented, key):
                    token_ok = True
                    break

        if not token_ok:
            if _sso_secret:
                client_secret = request.headers.get("x-gateway-secret", "")
                if not hmac.compare_digest(client_secret, _sso_secret):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            elif not _DEV_MODE:
                return JSONResponse({"error": "Service misconfigured"}, status_code=503)

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://assets.coingecko.com https://coin-images.coingecko.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    if _sso_secret:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# ─── Static / health ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/coin", response_class=HTMLResponse)
async def coin_page() -> HTMLResponse:
    return HTMLResponse(COIN_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/compare", response_class=HTMLResponse)
async def compare_page() -> HTMLResponse:
    return HTMLResponse(COMPARE_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/multipane", response_class=HTMLResponse)
async def multipane_page() -> HTMLResponse:
    return HTMLResponse(MULTIPANE_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page() -> HTMLResponse:
    return HTMLResponse(PRICING_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page() -> HTMLResponse:
    return HTMLResponse(WELCOME_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page() -> HTMLResponse:
    return HTMLResponse(PORTFOLIO_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/digest", response_class=HTMLResponse)
async def digest_page() -> HTMLResponse:
    return HTMLResponse(DIGEST_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page() -> HTMLResponse:
    return HTMLResponse(CHANGELOG_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/status", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    return HTMLResponse(STATUS_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/guide/pump-and-dump", response_class=HTMLResponse)
async def guide_pump_and_dump() -> HTMLResponse:
    return HTMLResponse(GUIDE_PUMP_PATH.read_text(encoding="utf-8"))


@app.get("/setups", response_class=HTMLResponse)
async def setups_page() -> HTMLResponse:
    return HTMLResponse(SETUPS_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "crypto-trackers", "ts": time.time()}


@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "service": "crypto-trackers", "ts": time.time()}


# ─── Universe & detail ────────────────────────────────────────────────────────

@app.get("/api/universe")
async def api_universe(top_n: int = 500) -> JSONResponse:
    return JSONResponse(coingecko.universe(top_n=top_n))


@app.get("/api/screener")
async def api_screener(
    top_n: int = 500,
    min_market_cap: float | None = None,
    min_volume: float | None = None,
    min_change_24h: float | None = None,
    max_change_24h: float | None = None,
    search: str | None = None,
    sort: str = "rank",
    order: str = "asc",
    limit: int = 100,
) -> JSONResponse:
    univ = coingecko.universe(top_n=top_n)
    rows = screener_mod.screen(
        univ.get("coins") or [],
        min_market_cap=min_market_cap,
        min_volume=min_volume,
        min_price_change_24h=min_change_24h,
        max_price_change_24h=max_change_24h,
        search=search,
        sort=sort, order=order, limit=limit,
    )
    return JSONResponse({
        "coins": rows,
        "count": len(rows),
        "filters_applied": {
            "min_market_cap": min_market_cap, "min_volume": min_volume,
            "min_change_24h": min_change_24h, "max_change_24h": max_change_24h,
            "search": search, "sort": sort, "order": order, "limit": limit,
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


@app.get("/api/coin/{coin_id}")
async def api_coin(coin_id: str) -> JSONResponse:
    return JSONResponse(coingecko.coin_detail(coin_id))


@app.get("/api/global")
async def api_global() -> JSONResponse:
    return JSONResponse(coingecko.global_metrics())


@app.get("/api/trending")
async def api_trending() -> JSONResponse:
    return JSONResponse(coingecko.trending())


# ─── Per-exchange ─────────────────────────────────────────────────────────────

@app.get("/api/binance/spot")
async def api_binance_spot() -> JSONResponse:
    return JSONResponse(binance.spot_ticker_24h())


@app.get("/api/binance/futures")
async def api_binance_futures() -> JSONResponse:
    return JSONResponse(binance.futures_ticker_24h())


@app.get("/api/binance/depth")
async def api_binance_depth(symbol: str = "BTCUSDT", limit: int = 50) -> JSONResponse:
    return JSONResponse(binance.spot_depth(symbol=symbol, limit=limit))


@app.get("/api/binance/klines")
async def api_binance_klines(symbol: str = "BTCUSDT",
                              interval: str = "1h",
                              limit: int = 168) -> JSONResponse:
    return JSONResponse(binance.klines(symbol=symbol, interval=interval, limit=limit))


@app.get("/api/binance/trades")
async def api_binance_trades(symbol: str = "BTCUSDT", limit: int = 200) -> JSONResponse:
    return JSONResponse(binance.recent_trades(symbol=symbol, limit=limit))


@app.get("/api/coinbase/{product_id}/ticker")
async def api_coinbase_ticker(product_id: str) -> JSONResponse:
    return JSONResponse(coinbase.ticker(product_id))


@app.get("/api/coinbase/{product_id}/stats")
async def api_coinbase_stats(product_id: str) -> JSONResponse:
    return JSONResponse(coinbase.stats(product_id))


@app.get("/api/kraken/ticker")
async def api_kraken_ticker(pair: str = "XBTUSD") -> JSONResponse:
    return JSONResponse(kraken.ticker(pair))


@app.get("/api/bybit/tickers")
async def api_bybit_tickers(category: str = "linear") -> JSONResponse:
    return JSONResponse(bybit.tickers(category))


@app.get("/api/okx/tickers")
async def api_okx_tickers(inst_type: str = "SWAP") -> JSONResponse:
    return JSONResponse(okx.tickers(inst_type))


# ─── DeFi / TVL ───────────────────────────────────────────────────────────────

@app.get("/api/defi/chains")
async def api_defi_chains() -> JSONResponse:
    return JSONResponse(defillama.chains())


@app.get("/api/defi/protocols")
async def api_defi_protocols(limit: int = 100) -> JSONResponse:
    return JSONResponse(defillama.protocols(limit=limit))


@app.get("/api/defi/stablecoins")
async def api_defi_stablecoins() -> JSONResponse:
    return JSONResponse(defillama.stablecoins())


@app.get("/api/defi/dexs")
async def api_defi_dexs() -> JSONResponse:
    return JSONResponse(defillama.dex_overview())


# ─── Sentiment / fear-greed ───────────────────────────────────────────────────

@app.get("/api/sentiment/fear_greed")
async def api_fng(days: int = 30) -> JSONResponse:
    return JSONResponse(fear_greed.index(days=days))


# ─── Cross-exchange & funding ─────────────────────────────────────────────────

@app.get("/api/cross_exchange/spreads")
async def api_cross_spreads(min_volume_usd: float = 500_000, top_n: int = 50) -> JSONResponse:
    spot = binance.spot_ticker_24h()
    by = bybit.tickers("spot")
    okx_spot = okx.tickers("SPOT")
    # Coinbase + Kraken: we only have per-pair ticker, so pull a curated set
    pairs_cb = ("BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "XRP-USD",
                "ADA-USD", "AVAX-USD", "DOT-USD", "MATIC-USD", "LINK-USD")
    pairs_kr = ("XBTUSD", "ETHUSD", "SOLUSD", "DOGEUSD", "XRPUSD",
                "ADAUSD", "AVAXUSD", "DOTUSD", "LINKUSD")
    cb = await asyncio.gather(*[_to_thread(coinbase.ticker, p) for p in pairs_cb])
    kr = await asyncio.gather(*[_to_thread(kraken.ticker, p) for p in pairs_kr])
    out = arb_mod.cross_exchange_spreads(
        binance_spot=spot, coinbase_tickers=list(cb), kraken_tickers=list(kr),
        bybit_tickers=by, okx_tickers=okx_spot,
        min_volume_usd=min_volume_usd, top_n=top_n,
    )
    return JSONResponse(out)


@app.get("/api/funding/rates")
async def api_funding_rates() -> JSONResponse:
    premium = binance.futures_premium_index()
    by = bybit.tickers("linear")
    okx_swap = okx.tickers("SWAP")
    return JSONResponse(funding_mod.collect(
        binance_premium=premium, bybit_tickers=by, okx_tickers=okx_swap,
    ))


# ─── News ─────────────────────────────────────────────────────────────────────

@app.get("/api/news")
async def api_news(limit: int = 60) -> JSONResponse:
    return JSONResponse(news.headlines(limit=max(5, min(limit, 200))))


# ─── Network metrics (BTC + ETH) ──────────────────────────────────────────────

@app.get("/api/network/btc")
async def api_network_btc() -> JSONResponse:
    return JSONResponse(mempool_btc.network_status())


@app.get("/api/network/btc/hashrate")
async def api_btc_hashrate() -> JSONResponse:
    return JSONResponse(mempool_btc.recent_hashrate())


@app.get("/api/network/eth/gas")
async def api_eth_gas() -> JSONResponse:
    return JSONResponse(etherscan_gas.eth_gas_oracle())


# ─── Liquidations ─────────────────────────────────────────────────────────────

@app.get("/api/liquidations/binance")
async def api_liq_binance(limit: int = 100) -> JSONResponse:
    return JSONResponse(binance_liquidations.recent_liquidations(limit=limit))


@app.get("/api/liquidations/okx")
async def api_liq_okx(limit: int = 100) -> JSONResponse:
    return JSONResponse(okx_liquidations.recent_liquidations(limit=limit))


@app.get("/api/liquidations/bybit")
async def api_liq_bybit() -> JSONResponse:
    return JSONResponse(bybit_liquidations.probable_liquidations())


@app.get("/api/liquidations/aggregate")
async def api_liq_aggregate() -> JSONResponse:
    bin_liq = binance_liquidations.recent_liquidations(100)
    okx_liq = okx_liquidations.recent_liquidations(100)
    by_liq = bybit_liquidations.probable_liquidations()
    return JSONResponse(liquidations_agg.aggregate(
        binance=bin_liq, okx=okx_liq, bybit=by_liq))


@app.get("/api/hyperliquid/market")
async def api_hyperliquid() -> JSONResponse:
    return JSONResponse(hyperliquid.market_state())


@app.get("/api/portfolio")
async def api_portfolio(addresses: str = "") -> JSONResponse:
    """Aggregate holdings across multiple BTC/ETH/SOL wallets.

    ``addresses`` is a comma-separated list.  Each address auto-detected
    by prefix heuristic; unknown formats are noted in the response."""
    addrs = [a.strip() for a in addresses.split(",") if a.strip()]
    if not addrs:
        return JSONResponse({"error": "no addresses supplied",
                             "hint": "?addresses=0x...,bc1...,..."}, status_code=400)
    if len(addrs) > 20:
        return JSONResponse({"error": "max 20 addresses per request"},
                            status_code=400)
    return JSONResponse(portfolio.aggregate(addrs))


@app.get("/api/portfolio/detect")
async def api_portfolio_detect(address: str = "") -> JSONResponse:
    """Quick chain-detection probe for client-side validation."""
    return JSONResponse({"address": address, "chain": portfolio.detect_chain(address)})


# ─── On-chain context per coin ────────────────────────────────────────────────

@app.get("/api/onchain/{coin_id}")
async def api_onchain(coin_id: str) -> JSONResponse:
    return JSONResponse(onchain_lookup.per_coin_context(coin_id))


@app.get("/api/onchain/chain/{chain}/gas")
async def api_chain_gas(chain: str) -> JSONResponse:
    return JSONResponse(etherscan_token.gas_oracle(chain))


@app.get("/api/onchain/sol/holders/{token}")
async def api_sol_holders(token: str, limit: int = 10) -> JSONResponse:
    return JSONResponse(solscan.top_holders(token, limit=limit))


# ─── Whales ───────────────────────────────────────────────────────────────────

@app.get("/api/whales/eth")
async def api_whales_eth() -> JSONResponse:
    return JSONResponse(whales.exchange_balances_eth())


@app.get("/api/whales/btc")
async def api_whales_btc(min_btc: float = 100.0) -> JSONResponse:
    return JSONResponse(whales.large_btc_transactions(min_btc=max(1.0, min_btc)))


# ─── Solana network ───────────────────────────────────────────────────────────

@app.get("/api/network/sol")
async def api_network_sol() -> JSONResponse:
    return JSONResponse(solana.network_status())


@app.get("/api/network/sol/fees")
async def api_sol_fees() -> JSONResponse:
    return JSONResponse(solana.priority_fees())


@app.get("/api/network/sol/validators")
async def api_sol_validators() -> JSONResponse:
    return JSONResponse(solana.validator_summary())


@app.get("/api/network/sol/jito_tips")
async def api_jito_tips() -> JSONResponse:
    return JSONResponse(jito.tip_floor())


# ─── DEX prices ───────────────────────────────────────────────────────────────

@app.get("/api/dex/prices")
async def api_dex_prices() -> JSONResponse:
    return JSONResponse(defillama_prices.cross_dex_prices())


# ─── BTC treasuries ───────────────────────────────────────────────────────────

@app.get("/api/btc/treasuries")
async def api_btc_treasuries() -> JSONResponse:
    return JSONResponse(btc_treasuries.holdings_table())


# ─── Derivatives (Deribit options) ────────────────────────────────────────────

@app.get("/api/deribit/{currency}")
async def api_deribit(currency: str) -> JSONResponse:
    cur = currency.upper()
    if cur not in {"BTC", "ETH", "SOL"}:
        cur = "BTC"
    return JSONResponse(deribit.market_overview(cur))


# ─── Macro cross-asset ────────────────────────────────────────────────────────

@app.get("/api/macro")
async def api_macro() -> JSONResponse:
    return JSONResponse(macro.snapshot())


@app.get("/api/macro/correlations")
async def api_macro_corr() -> JSONResponse:
    return JSONResponse(macro.btc_correlation_30d())


# ─── Sectors ──────────────────────────────────────────────────────────────────

@app.get("/api/sectors")
async def api_sectors() -> JSONResponse:
    univ = coingecko.universe(500)
    coins = univ.get("coins") or []
    grouped = sectors_mod.group(coins)
    return JSONResponse({
        **grouped,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Bridges ──────────────────────────────────────────────────────────────────

@app.get("/api/bridges")
async def api_bridges() -> JSONResponse:
    return JSONResponse(defillama_bridges.overview())


# ─── Trader tools: LP/IL + DCA simulator ──────────────────────────────────────

@app.get("/api/tools/il")
async def api_tools_il(
    price_ratio: float = 1.0,
    v3_range_low: Optional[float] = None,
    v3_range_high: Optional[float] = None,
) -> JSONResponse:
    """Impermanent-loss calculator. V2 always returned; V3 only when range given."""
    out: dict = {
        "price_ratio": price_ratio,
        "price_change_pct": (price_ratio - 1) * 100,
        "il_v2_pct": round(lp_il.il_pct_v2(price_ratio) or 0, 4),
        "scenario_grid": lp_il.il_grid(),
    }
    if v3_range_low is not None and v3_range_high is not None:
        out["il_v3_pct"] = round(
            lp_il.il_pct_v3(price_ratio, v3_range_low, v3_range_high) or 0, 4)
        out["v3_range_low"] = v3_range_low
        out["v3_range_high"] = v3_range_high
    return JSONResponse(out)


@app.get("/api/tools/dca")
async def api_tools_dca(
    symbol: str = "BTCUSDT",
    buy_usd: float = 100.0,
    every_n_days: int = 7,
    lookback_days: int = 365,
) -> JSONResponse:
    lookback_days = max(7, min(lookback_days, 1000))
    kl = binance.klines(symbol=symbol, interval="1d", limit=lookback_days)
    if kl.get("error"):
        return JSONResponse({"error": kl["error"], "symbol": symbol}, status_code=502)
    out = dca_simulator.simulate(
        kl.get("bars") or [],
        buy_usd=buy_usd,
        every_n_days=every_n_days,
        lookback_days=lookback_days,
    )
    out["symbol"] = symbol
    return JSONResponse(out)


@app.get("/api/tools/backtest")
async def api_tools_backtest(
    symbol: str = "BTCUSDT",
    strategy: str = "sma_crossover",
    lookback_days: int = 365,
    starting_usd: float = 10_000,
    fee_pct: float = 0.1,
    fast: int = 50,
    slow: int = 200,
    rsi_period: int = 14,
    rsi_oversold: float = 30,
    rsi_overbought: float = 70,
    breakout_lookback: int = 20,
) -> JSONResponse:
    lookback_days = max(30, min(lookback_days, 1000))
    kl = binance.klines(symbol=symbol, interval="1d", limit=lookback_days)
    if kl.get("error"):
        return JSONResponse({"error": kl["error"], "symbol": symbol}, status_code=502)
    bars = kl.get("bars") or []
    if strategy == "sma_crossover":
        out = dca_simulator.sma_crossover(bars, fast=fast, slow=slow,
                                           starting_usd=starting_usd, fee_pct=fee_pct)
    elif strategy == "rsi_mean_reversion":
        out = dca_simulator.rsi_mean_reversion(bars, period=rsi_period,
                                                oversold=rsi_oversold, overbought=rsi_overbought,
                                                starting_usd=starting_usd, fee_pct=fee_pct)
    elif strategy == "breakout":
        out = dca_simulator.breakout(bars, lookback=breakout_lookback,
                                      starting_usd=starting_usd, fee_pct=fee_pct)
    else:
        return JSONResponse({"error": f"unknown strategy {strategy}",
                              "supported": ["sma_crossover", "rsi_mean_reversion", "breakout"]},
                             status_code=400)
    out["symbol"] = symbol
    return JSONResponse(out)


@app.get("/api/tools/size")
async def api_tools_size(
    account_usd: float = 10_000.0,
    risk_pct: float = 1.0,
    entry: float = 0.0,
    stop: float = 0.0,
    target: Optional[float] = None,
    leverage: float = 1.0,
    fee_pct: float = 0.1,
) -> JSONResponse:
    return JSONResponse(position_sizer.size(
        account_usd=account_usd, risk_pct=risk_pct, entry=entry, stop=stop,
        target=target, leverage=leverage, fee_pct=fee_pct,
    ))


@app.post("/api/tools/tax_lots")
async def api_tools_tax_lots(req: Request) -> JSONResponse:
    """POST a trade-journal JSON array; receive FIFO-matched lots with
    realised P&L + short/long-term split + CSV-ready download.

    Body: {"trades": [...]}. No server-side state — the client (trade
    journal in localStorage) is the source of truth."""
    try:
        body = await req.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    trades = body.get("trades") if isinstance(body, dict) else body
    if not isinstance(trades, list):
        return JSONResponse({"error": "trades must be a JSON array"}, status_code=400)
    return JSONResponse(tax_lots.realised_pnl_fifo(trades))


@app.post("/api/tools/tax_lots/csv")
async def api_tools_tax_lots_csv(req: Request):
    """Same as /api/tools/tax_lots but returns CSV ready for IRS Form 8949."""
    from fastapi.responses import PlainTextResponse
    try:
        body = await req.json()
    except (ValueError, TypeError):
        return PlainTextResponse("error: invalid JSON body", status_code=400)
    trades = body.get("trades") if isinstance(body, dict) else body
    if not isinstance(trades, list):
        return PlainTextResponse("error: trades must be a JSON array", status_code=400)
    result = tax_lots.realised_pnl_fifo(trades)
    return PlainTextResponse(tax_lots.to_csv(result.get("matches", [])),
                              headers={"Content-Disposition": 'attachment; filename="tax-lots.csv"'})


# ─── L2 sequencer revenue + restaking ─────────────────────────────────────────

@app.get("/api/defi/l2_fees")
async def api_l2_fees() -> JSONResponse:
    return JSONResponse(defillama_fees.l2_sequencer_revenue())


@app.get("/api/defi/restaking")
async def api_restaking() -> JSONResponse:
    return JSONResponse(defillama_fees.restaking_protocols())


# ─── Token unlocks ────────────────────────────────────────────────────────────

@app.get("/api/unlocks")
async def api_unlocks(horizon_days: int = 90) -> JSONResponse:
    return JSONResponse(token_unlocks.upcoming(
        horizon_days=max(7, min(horizon_days, 365))))


# ─── Pump.fun memecoin trending ───────────────────────────────────────────────

@app.get("/api/pumpfun")
async def api_pumpfun(limit: int = 30) -> JSONResponse:
    return JSONResponse(pumpfun.trending(limit=limit))


# ─── Hacks ────────────────────────────────────────────────────────────────────

@app.get("/api/hacks")
async def api_hacks() -> JSONResponse:
    return JSONResponse(rekt_hacks.hacks_overview())


# ─── NFT floors ───────────────────────────────────────────────────────────────

@app.get("/api/nft/floors")
async def api_nft_floors(limit: int = 30) -> JSONResponse:
    return JSONResponse(nft_floors.top_collections(limit=limit))


# ─── DeFi yields ──────────────────────────────────────────────────────────────

@app.get("/api/defi/yields")
async def api_defi_yields(min_tvl: float = 1_000_000, limit: int = 100,
                          stable: bool = False,
                          no_il: bool = False) -> JSONResponse:
    return JSONResponse(defillama_yields.top_yields(
        min_tvl_usd=min_tvl, limit=max(5, min(limit, 500)),
        stablecoin_only=stable, max_il_risk="no" if no_il else None,
    ))


# ─── Stablecoin peg ───────────────────────────────────────────────────────────

@app.get("/api/stablecoins/peg")
async def api_stable_peg() -> JSONResponse:
    return JSONResponse(stablecoin_peg.peg_status())


# ─── DEX vs CEX premium ───────────────────────────────────────────────────────

@app.get("/api/dex_cex/premium")
async def api_dex_cex_premium() -> JSONResponse:
    dex = defillama_prices.cross_dex_prices()
    cex = binance.spot_ticker_24h()
    return JSONResponse(dex_cex_premium.compute(dex_prices=dex, binance_spot=cex))


# ─── Funding carry ────────────────────────────────────────────────────────────

@app.get("/api/funding/carry")
async def api_funding_carry() -> JSONResponse:
    premium = binance.futures_premium_index()
    by = bybit.tickers("linear")
    okx_swap = okx.tickers("SWAP")
    fr = funding_mod.collect(binance_premium=premium, bybit_tickers=by,
                              okx_tickers=okx_swap)
    enriched_rows = carry_mod.enrich_funding_rows(fr.get("rows_top") or [])
    enriched_rows.sort(key=lambda r: abs(r.get("carry_pct_annualised") or 0),
                       reverse=True)
    return JSONResponse({
        "rows_top": enriched_rows[:30],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Alerts ───────────────────────────────────────────────────────────────────

@app.get("/api/alerts")
async def api_alerts_list() -> JSONResponse:
    return JSONResponse(alerts_mod.summary())


@app.post("/api/alerts")
async def api_alerts_create(req: Request) -> JSONResponse:
    try:
        body = await req.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    result = alerts_mod.create_alert(
        alert_type=body.get("type", ""),
        target=str(body.get("target", "")),
        threshold=float(body.get("threshold", 0)),
        webhook_url=body.get("webhook_url"),
        cooldown_s=int(body.get("cooldown_s", 600)),
        label=body.get("label"),
    )
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@app.delete("/api/alerts/{alert_id}")
async def api_alerts_delete(alert_id: str) -> JSONResponse:
    ok = alerts_mod.delete_alert(alert_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "id": alert_id})


@app.post("/api/alerts/{alert_id}/toggle")
async def api_alerts_toggle(alert_id: str, req: Request) -> JSONResponse:
    try:
        body = await req.json()
    except (ValueError, TypeError):
        body = {}
    enabled = bool(body.get("enabled", True))
    ok = alerts_mod.toggle_alert(alert_id, enabled)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "id": alert_id, "enabled": enabled})


@app.post("/api/alerts/check")
async def api_alerts_check() -> JSONResponse:
    """Manually trigger an alert-check cycle. Useful for testing without
    waiting for the background loop."""
    fired = alerts_mod.check_all()
    return JSONResponse({"fired_count": len(fired), "fired": fired,
                         "fetched_at": datetime.now(timezone.utc).isoformat()})


# ─── WebSocket live tick ──────────────────────────────────────────────────────

@app.websocket("/ws/prices")
async def ws_prices(ws: WebSocket):
    """Live tick broadcast: prices + F&G + BTC mempool + BTC funding.

    Note: SSO middleware doesn't run on websocket handshakes (Starlette
    routes WS through a separate path). If you're putting this behind
    the gateway, ensure the gateway forwards the Upgrade header without
    requiring x-gateway-secret on the WS handshake.
    """
    try:
        await ws_broadcaster.register(ws)
    except Exception as e:  # noqa: BLE001
        log.warning("WS accept failed: %s", e)
        return
    try:
        # Send an initial tick immediately so the client gets data fast
        try:
            await ws.send_json(ws_broadcaster._build_tick())
        except Exception:  # noqa: BLE001
            pass
        while True:
            # Keep the connection alive; the broadcaster pushes ticks
            # asynchronously. We just discard whatever the client sends.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_broadcaster.unregister(ws)


@app.get("/api/ws/status")
async def api_ws_status() -> JSONResponse:
    return JSONResponse({
        "ws_clients": ws_broadcaster.client_count(),
        "broadcast_path": "/ws/prices",
    })


# ─── Per-source health + persisted cache ──────────────────────────────────────

@app.get("/api/sources")
async def api_sources() -> JSONResponse:
    return JSONResponse({
        "sources": _health.all_sources(),
        "persisted_cache": _persistence.all_entries(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Single-shot dashboard summary ────────────────────────────────────────────

async def _to_thread(fn, *args, **kwargs):
    return await asyncio.get_event_loop().run_in_executor(None, lambda: fn(*args, **kwargs))


@app.get("/api/summary")
async def api_summary() -> JSONResponse:
    """Single payload for the front page. Fans out to ~12 upstreams in
    parallel so a slow venue doesn't dominate page-load latency."""
    (univ, glob, trend, fng, chains, dexs, stables, fut, premium,
     binance_spot, bybit_spot, okx_spot,
     news_headlines, btc_net, eth_gas, liq, okx_liq, dex_prices, treasuries,
     sol_net, whales_eth, whales_btc, hl_market, bybit_liq) = await asyncio.gather(
        _to_thread(coingecko.universe, 200),
        _to_thread(coingecko.global_metrics),
        _to_thread(coingecko.trending),
        _to_thread(fear_greed.index, 14),
        _to_thread(defillama.chains),
        _to_thread(defillama.dex_overview),
        _to_thread(defillama.stablecoins),
        _to_thread(binance.futures_ticker_24h),
        _to_thread(binance.futures_premium_index),
        _to_thread(binance.spot_ticker_24h),
        _to_thread(bybit.tickers, "spot"),
        _to_thread(okx.tickers, "SPOT"),
        _to_thread(news.headlines, 30),
        _to_thread(mempool_btc.network_status),
        _to_thread(etherscan_gas.eth_gas_oracle),
        _to_thread(binance_liquidations.recent_liquidations, 100),
        _to_thread(okx_liquidations.recent_liquidations, 100),
        _to_thread(defillama_prices.cross_dex_prices),
        _to_thread(btc_treasuries.holdings_table),
        _to_thread(solana.network_status),
        _to_thread(whales.exchange_balances_eth),
        _to_thread(whales.large_btc_transactions, 100.0),
        _to_thread(hyperliquid.market_state),
        _to_thread(bybit_liquidations.probable_liquidations),
    )

    # Quick aggregate: top 10 movers (gainers + losers) from the universe
    coins = univ.get("coins") or []
    top_gainers = sorted([c for c in coins if c.get("change_24h") is not None],
                          key=lambda c: c["change_24h"], reverse=True)[:10]
    top_losers = sorted([c for c in coins if c.get("change_24h") is not None],
                         key=lambda c: c["change_24h"])[:10]

    # Top funding rates from binance premium (long & short)
    prem_rows = (premium.get("rows") or [])
    top_funding_long = sorted([r for r in prem_rows if r.get("funding_rate") is not None],
                                key=lambda r: r["funding_rate"], reverse=True)[:8]
    top_funding_short = sorted([r for r in prem_rows if r.get("funding_rate") is not None],
                                 key=lambda r: r["funding_rate"])[:8]

    return JSONResponse({
        "global": glob,
        "trending": trend,
        "fear_greed_latest": (fng.get("latest") if not fng.get("error") else None),
        "fear_greed_recent": fng.get("rows", [])[:14],
        "top_gainers_24h": top_gainers,
        "top_losers_24h": top_losers,
        "top_funding_long": top_funding_long,
        "top_funding_short": top_funding_short,
        "defi": {
            "chains_count": chains.get("count", 0),
            "total_tvl_usd": chains.get("total_tvl_usd", 0),
            "top_chains": (chains.get("chains") or [])[:10],
            "dex_24h_usd": dexs.get("total_24h_usd"),
            "stablecoin_total_usd": stables.get("total_circulating_usd"),
            "top_stablecoins": (stables.get("stablecoins") or [])[:6],
        },
        "exchange_health": {
            "binance_spot_symbols": (binance_spot.get("count") if not binance_spot.get("error") else 0),
            "binance_futures_symbols": (fut.get("count") if not fut.get("error") else 0),
            "bybit_spot_symbols": (bybit_spot.get("count") if not bybit_spot.get("error") else 0),
            "okx_spot_symbols": (okx_spot.get("count") if not okx_spot.get("error") else 0),
        },
        "universe_count": univ.get("count", 0),
        "news_top": (news_headlines.get("headlines") or [])[:10],
        "news_count": news_headlines.get("count", 0),
        "network": {
            "btc": btc_net,
            "eth_gas": eth_gas,
            "sol": sol_net,
        },
        "liquidations": {
            "count": liq.get("count", 0),
            "total_notional_usd": (liq.get("total_notional_usd", 0) or 0)
                                  + (okx_liq.get("total_notional_usd", 0) or 0),
            "biggest": liq.get("biggest") or okx_liq.get("biggest"),
            "by_symbol_top": liquidations_agg.aggregate(
                binance=liq, okx=okx_liq, bybit=bybit_liq
            ).get("rows", [])[:8],
            "venues_used": ["binance", "okx", "bybit (proxy)"],
        },
        "hyperliquid": {
            "count": hl_market.get("count", 0),
            "top_volume": (hl_market.get("rows") or [])[:8],
            "error": hl_market.get("error"),
        },
        "dex_prices": dex_prices,
        "whales": {
            "eth": {
                "total_balance_eth": whales_eth.get("total_balance_eth"),
                "net_flow_eth": whales_eth.get("net_flow_eth"),
                "significant_moves": whales_eth.get("significant_moves", [])[:5],
                "error": whales_eth.get("error"),
            },
            "btc": {
                "count": whales_btc.get("count", 0),
                "total_btc": whales_btc.get("total_btc"),
                "biggest_btc": whales_btc.get("biggest_btc"),
                "top": (whales_btc.get("rows") or [])[:6],
                "error": whales_btc.get("error"),
            },
        },
        "btc_treasuries": {
            "total_tracked_btc": treasuries.get("total_tracked_btc"),
            "pct_of_supply_tracked": treasuries.get("pct_of_supply_tracked"),
            "by_type": treasuries.get("by_type"),
            "top_holdings": (treasuries.get("holdings") or [])[:10],
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Background pre-fetch ─────────────────────────────────────────────────────

@app.on_event("startup")
async def _startup_ws() -> None:
    ws_broadcaster.start()


@app.on_event("startup")
async def _startup() -> None:
    _background.start([
        ("coingecko_universe",    lambda: coingecko.universe(500),       60),
        ("coingecko_global",      lambda: coingecko.global_metrics(),    120),
        ("coingecko_trending",    lambda: coingecko.trending(),          300),
        ("binance_spot_24h",      lambda: binance.spot_ticker_24h(),      30),
        ("binance_futures_24h",   lambda: binance.futures_ticker_24h(),   30),
        ("binance_premium",       lambda: binance.futures_premium_index(),30),
        ("bybit_linear",          lambda: bybit.tickers("linear"),        30),
        ("bybit_spot",            lambda: bybit.tickers("spot"),          30),
        ("okx_swap",              lambda: okx.tickers("SWAP"),            30),
        ("okx_spot",              lambda: okx.tickers("SPOT"),            30),
        ("defillama_chains",      lambda: defillama.chains(),             900),
        ("defillama_protocols",   lambda: defillama.protocols(150),       900),
        ("defillama_dexs",        lambda: defillama.dex_overview(),       900),
        ("defillama_stables",     lambda: defillama.stablecoins(),        1800),
        ("fear_greed",            lambda: fear_greed.index(30),           3600),
        ("news",                  lambda: news.headlines(60),             600),
        ("mempool_btc",           lambda: mempool_btc.network_status(),   60),
        ("mempool_hashrate",      lambda: mempool_btc.recent_hashrate(),  3600),
        ("eth_gas",               lambda: etherscan_gas.eth_gas_oracle(), 60),
        ("binance_liq",           lambda: binance_liquidations.recent_liquidations(100), 60),
        ("okx_liq",               lambda: okx_liquidations.recent_liquidations(100),     60),
        ("solana_network",        lambda: solana.network_status(),                       60),
        ("solana_priority_fees",  lambda: solana.priority_fees(),                        60),
        ("solana_validators",     lambda: solana.validator_summary(),                    3600),
        ("jito_tip_floor",        lambda: jito.tip_floor(),                              60),
        ("whales_eth",            lambda: whales.exchange_balances_eth(),                300),
        ("whales_btc",            lambda: whales.large_btc_transactions(100),            120),
        ("hyperliquid",           lambda: hyperliquid.market_state(),                    60),
        ("bybit_proxy_liq",       lambda: bybit_liquidations.probable_liquidations(),    120),
        ("llama_cross_dex",       lambda: defillama_prices.cross_dex_prices(),  60),
        ("alerts_check",          lambda: alerts_mod.check_all(),                       30),
        ("deribit_btc",           lambda: deribit.market_overview("BTC"),               60),
        ("deribit_eth",           lambda: deribit.market_overview("ETH"),               60),
        ("macro_snapshot",        lambda: macro.snapshot(),                             300),
        ("defillama_yields",      lambda: defillama_yields.top_yields(1_000_000, 100),  900),
        ("stablecoin_peg",        lambda: stablecoin_peg.peg_status(),                  60),
        ("defillama_bridges",     lambda: defillama_bridges.overview(),                 900),
        ("rekt_hacks",            lambda: rekt_hacks.hacks_overview(),                  3600),
        ("nft_floors",            lambda: nft_floors.top_collections(30),               600),
        ("defillama_l2_fees",     lambda: defillama_fees.l2_sequencer_revenue(),        1800),
        ("defillama_restaking",   lambda: defillama_fees.restaking_protocols(),         900),
        ("pumpfun_trending",      lambda: pumpfun.trending(50),                         60),
    ])


@app.on_event("shutdown")
async def _shutdown() -> None:
    _background.stop()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7054"))
    log.info("Starting crypto-trackers dashboard on :%d", port)
    uvicorn.run(app, host=os.environ.get("BIND_HOST", "0.0.0.0"), port=port)

#!/usr/bin/env python3
"""
FastAPI backend serving the crypto dashboard via REST + WebSocket.
Powers both the web dashboard and the iOS app.
"""

import asyncio
import json
import time
import math
import os
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict
import html as html_mod
import defusedxml.ElementTree as ET

import requests
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from btc_analyzer import (
    ASSETS, WINDOW_MINUTES, WINDOW_SECONDS, HISTORY_DAYS,
    load_or_fetch, parse_klines, analyze_windows,
    compute_summary, compute_volatility, compute_per_second_velocity,
    EnsemblePredictor, generate_dashboard,
)
import database as db

app = FastAPI(title="CryptoEdge", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # No CORS — no API
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─── Security Middleware ─────────────────────────────────────────────
_request_counts: dict[str, list[float]] = {}
RATE_LIMIT = 120  # requests per minute per IP
RATE_WINDOW = 60
_last_prune = 0.0
_PRUNE_INTERVAL = 300  # prune stale IPs every 5 minutes


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    global _last_prune
    # Rate limiting per IP
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    # Prune departed IPs periodically; hard-cap to prevent unbounded growth
    if now - _last_prune > _PRUNE_INTERVAL:
        _last_prune = now
        stale = [k for k, v in _request_counts.items() if not v or now - v[-1] > RATE_WINDOW]
        for k in stale:
            del _request_counts[k]
        if len(_request_counts) > 10000:
            _request_counts.clear()
        # _login_attempts pruning removed -- login rate limiting is handled by gateway
    reqs = _request_counts.get(ip, [])
    reqs = [t for t in reqs if now - t < RATE_WINDOW]
    if len(reqs) >= RATE_LIMIT and ip not in ("127.0.0.1", "::1"):
        return JSONResponse({"error": "Rate limit exceeded"}, status_code=429)
    reqs.append(now)
    _request_counts[ip] = reqs

    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# ─── Authentication ──────────────────────────────────────────────────
# Auth is handled by the gateway. These helpers extract user info from
# gateway SSO headers or allow localhost bypass for trading bots.


def _get_session_user(request: Request) -> dict | None:
    """Get the authenticated user from gateway SSO headers."""
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    if _sso_secret and hmac.compare_digest(request.headers.get("x-gateway-secret", ""), _sso_secret):
        gw_id = request.headers.get("x-gateway-user-id")
        gw_email = request.headers.get("x-gateway-user-email")
        if gw_id and gw_email:
            return {
                "id": gw_id,
                "email": gw_email,
                "tier": "admin",
                "display_name": gw_email.split("@")[0],
            }

    # Localhost bypass for trading bots — only when explicitly enabled via env var.
    if os.environ.get("DEV_LOCALHOST_BYPASS", "").strip() == "1":
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            return {"id": "00000000-0000-0000-0000-000000000000", "email": "localhost", "tier": "admin", "display_name": "System"}
    return None


def _check_auth(request: Request) -> bool:
    return _get_session_user(request) is not None


def _is_premium(request: Request) -> bool:
    user = _get_session_user(request)
    return user is not None and user["tier"] in ("premium", "admin")


async def require_auth(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ─── In-memory state ─────────────────────────────────────────────────
asset_state = {}       # ticker -> full result dict
ensembles = {}         # ticker -> trained EnsemblePredictor
live_prices = {}       # ticker -> latest price
connected_ws = set()   # active WebSocket connections
last_refresh = {}      # ticker -> timestamp of last full refresh
REFRESH_INTERVAL = 300 # re-analyze every 5 min (1 window)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"


# ─── Startup: load cached data + train models ────────────────────────
@app.on_event("startup")
async def startup():
    db.init_db()
    # Start background tasks immediately so dashboards work
    asyncio.create_task(price_updater())
    asyncio.create_task(window_refresher())
    asyncio.create_task(suspicious_trade_monitor())
    asyncio.create_task(news_trade_monitor())
    # Load data in background so server is available immediately
    asyncio.create_task(load_all_assets())
    print("Server started. Loading data in background...")


async def load_all_assets():
    """Load all assets in background so server can serve pages immediately."""
    print(f"Loading {HISTORY_DAYS}d data and training models...")

    # Phase 1: Load all windows FIRST so all tabs appear on dashboard
    all_windows = {}
    for ticker, info in ASSETS.items():
        try:
            await asyncio.to_thread(load_asset_windows, ticker, info["symbol"])
            print(f"  {ticker} windows ready.")
        except Exception as e:
            print(f"  {ticker} failed to load: {e}")
    print("All asset windows loaded — dashboard ready.")

    # Phase 2: Train models for each asset (slow part, dashboard already usable)
    for ticker, info in ASSETS.items():
        if ticker not in asset_state:
            continue
        try:
            await asyncio.to_thread(train_asset_models, ticker)
            print(f"  {ticker} models trained.")
        except Exception as e:
            print(f"  {ticker} training failed: {e}")
    print("All models trained.")


def load_asset_windows(ticker, symbol):
    """Load cached data and analyze windows (fast part)."""
    import gc
    raw, start_dt, end_dt = load_or_fetch(symbol, days=HISTORY_DAYS)
    data = parse_klines(raw)
    del raw  # free ~400MB JSON
    gc.collect()

    windows = analyze_windows(data)
    summary = compute_summary(windows)
    volatility = compute_volatility(windows, lookback_hours=24)
    velocity = compute_per_second_velocity(data, windows)

    # Only keep last 2h of raw data for live updates (not all 30d)
    recent_data = data[-14400:] if len(data) > 14400 else data  # last 4h
    # Only keep last 1000 windows
    recent_windows = windows[-1000:] if len(windows) > 1000 else windows

    asset_state[ticker] = {
        "windows": recent_windows,
        "summary": summary,
        "volatility": volatility,
        "velocity": velocity,
        "backtest": None,
        "predictions": None,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "data": recent_data,
        "_all_windows": windows,  # keep for training phase
    }
    last_refresh[ticker] = time.time()

    del data
    gc.collect()


MODEL_CACHE_DIR = Path(__file__).parent / "cache" / "models"

def train_asset_models(ticker):
    """Train ensemble models for an asset (slow GPU part). Uses cached models if available."""
    import gc
    windows = asset_state[ticker].pop("_all_windows", None)
    if windows is None:
        return

    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_CACHE_DIR / f"{ticker}_ensemble.json"

    # Try loading cached model first
    ensemble = EnsemblePredictor.load_from_file(model_path)
    if ensemble:
        print(f"  {ticker} loaded cached model ({len(ensemble.models)} models)")
    else:
        # Train from scratch
        ensemble = EnsemblePredictor()
        ensemble.train_all(windows, verbose=False)
        # Save for next time
        try:
            ensemble.save_to_file(model_path)
            print(f"  {ticker} model saved to cache")
        except Exception as e:
            print(f"  {ticker} model save failed: {e}")

    bt = ensemble.backtest(windows)
    preds = ensemble.predict_current_and_recent(windows)

    ensembles[ticker] = ensemble
    asset_state[ticker]["backtest"] = bt
    asset_state[ticker]["predictions"] = preds
    asset_state[ticker]["model_info"] = ensemble.model_info

    del windows
    gc.collect()


async def price_updater():
    """Connect to Binance WebSocket stream for real-time prices, with REST fallback."""
    import aiohttp
    global connected_ws

    # Build combined stream URL for all assets
    # e.g. wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/...
    symbols_lower = [info["symbol"].lower() for info in ASSETS.values()]
    symbol_to_ticker = {info["symbol"]: ticker for ticker, info in ASSETS.items()}
    streams = "/".join(f"{s}@miniTicker" for s in symbols_lower)
    ws_url = f"wss://stream.binance.com:9443/stream?streams={streams}"

    last_push = 0
    pending_update: dict = {}

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, heartbeat=20) as ws:
                    print("[WS] Connected to Binance stream")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            payload = data.get("data", {})
                            symbol = payload.get("s", "")  # e.g. "BTCUSDT"
                            price = float(payload.get("c", 0))  # close price

                            if symbol in symbol_to_ticker and price > 0:
                                ticker = symbol_to_ticker[symbol]
                                live_prices[ticker] = price
                                pending_update[ticker] = {
                                    "price": price,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }

                            # Batch push to clients every 1 second (avoid flooding)
                            now = time.time()
                            if pending_update and connected_ws and now - last_push >= 1.0:
                                ws_msg = json.dumps({"type": "price_update", "data": pending_update})
                                dead = set()
                                for client_ws in list(connected_ws):
                                    try:
                                        await client_ws.send_text(ws_msg)
                                    except Exception:
                                        dead.add(client_ws)
                                for d in dead:
                                    connected_ws.discard(d)
                                pending_update = {}
                                last_push = now

                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
        except Exception as e:
            print(f"[WS] Binance stream error: {e}, reconnecting in 5s...")

        # Fallback: fetch REST prices while reconnecting
        try:
            resp = await asyncio.to_thread(requests.get, BINANCE_TICKER_URL, params={}, timeout=5)
            if resp.ok:
                all_prices = {p["symbol"]: float(p["price"]) for p in resp.json()}
                for ticker, info in ASSETS.items():
                    if info["symbol"] in all_prices:
                        live_prices[ticker] = all_prices[info["symbol"]]
        except Exception:
            pass
        await asyncio.sleep(5)


async def window_refresher():
    """Re-analyze windows and update predictions every 5 minutes."""
    global connected_ws
    while True:
        await asyncio.sleep(60)  # check every minute
        now = time.time()
        for ticker, info in ASSETS.items():
            if ticker not in asset_state:
                continue
            if now - last_refresh.get(ticker, 0) < REFRESH_INTERVAL:
                continue

            try:
                # Fetch latest 10 minutes of data to append
                symbol = info["symbol"]
                end_ms = int(now * 1000)
                start_ms = end_ms - (600 * 1000)  # last 10 min
                params = {
                    "symbol": symbol, "interval": "1s",
                    "startTime": start_ms, "endTime": end_ms, "limit": 1000,
                }
                resp = await asyncio.to_thread(
                    requests.get, BINANCE_KLINE_URL, params=params, timeout=15
                )
                if not resp.ok:
                    continue

                new_klines = resp.json()
                new_data = [(k[0], float(k[4])) for k in new_klines]

                # Merge with existing data (dedup by timestamp)
                existing = asset_state[ticker]["data"]
                existing_ts = {ts for ts, _ in existing}
                for ts, price in new_data:
                    if ts not in existing_ts:
                        existing.append((ts, price))
                existing.sort(key=lambda x: x[0])
                # Keep bounded (slice assignment to mutate the original list in-place)
                if len(existing) > 20000:
                    existing[:] = existing[-20000:]

                # Re-analyze the recent data to get latest windows
                new_windows = analyze_windows(existing)

                # Merge new windows into stored windows
                old_windows = asset_state[ticker]["windows"]
                # Find latest stored window time
                if old_windows:
                    last_stored = old_windows[-1]["start"]
                    for w in new_windows:
                        if w["start"] > last_stored:
                            old_windows.append(w)
                    # Keep bounded (slice assignment to mutate the original list in-place)
                    if len(old_windows) > 1000:
                        old_windows[:] = old_windows[-1000:]
                else:
                    old_windows = new_windows[-500:]

                # Update predictions using the full window history
                if ticker not in ensembles:
                    # Still save the windows even if models aren't trained yet
                    asset_state[ticker].update({"windows": old_windows})
                    continue
                preds = ensembles[ticker].predict_current_and_recent(old_windows)

                # Log predictions to DB for accuracy tracking
                for p in preds:
                    ws = p.get("window_start")
                    if ws and hasattr(ws, "isoformat"):
                        ws_str = ws.isoformat()
                    else:
                        ws_str = str(ws) if ws else ""
                    if p.get("is_current"):
                        db.log_prediction(
                            ticker=ticker, window_start=ws_str,
                            pred_direction=p["pred_direction"],
                            pred_delta=p["pred_end_delta"],
                            pred_prob=p["pred_prob_positive"],
                            confidence=p.get("confidence", 0),
                            ensemble_agreement=p.get("ensemble_agreement", ""),
                        )
                    elif p.get("actual_direction"):
                        db.resolve_prediction(
                            ticker=ticker, window_start=ws_str,
                            actual_direction=p["actual_direction"],
                            actual_delta=p.get("actual_end_delta", 0) or 0,
                        )

                asset_state[ticker].update({
                    "windows": old_windows,
                    "predictions": preds,
                    "data": existing,
                })
                last_refresh[ticker] = now

                # Push update to WebSocket clients
                if connected_ws:
                    msg = json.dumps({
                        "type": "window_update",
                        "ticker": ticker,
                        "data": serialize_asset(ticker),
                    })
                    dead = set()
                    for ws in list(connected_ws):
                        try:
                            await ws.send_text(msg)
                        except Exception:
                            dead.add(ws)
                    for d in dead:
                        connected_ws.discard(d)

                # Push high-confidence signal alerts (browser + email)
                if preds:
                    for p in preds:
                        if p.get("is_current") and p.get("confidence", 0) >= 0.6:
                            conf = int(p["confidence"] * 100)
                            delta_str = f'{p["pred_end_delta"]:+,.2f}'

                            # Browser push
                            if connected_ws:
                                alert_msg = json.dumps({
                                    "type": "alert",
                                    "data": {
                                        "ticker": ticker,
                                        "direction": p["pred_direction"],
                                        "confidence": conf,
                                        "delta": delta_str,
                                        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                                    },
                                })
                                dead = []
                                for ws in list(connected_ws):
                                    try:
                                        await ws.send_text(alert_msg)
                                    except Exception:
                                        dead.append(ws)
                                for ws in dead:
                                    connected_ws.discard(ws)

                            # Email alerts to users who opted in
                            try:
                                from email_alerts import send_alert_email, is_configured
                                if is_configured():
                                    # Get all users with email alerts enabled for this ticker
                                    prefs = db.get_alert_prefs_for_ticker(ticker) if hasattr(db, 'get_alert_prefs_for_ticker') else []
                                    for pref in prefs:
                                        if pref.get("alert_email") and p["confidence"] >= pref.get("min_confidence", 0.6):
                                            user = db.get_user(pref["user_id"])
                                            if user:
                                                await asyncio.to_thread(
                                                    send_alert_email,
                                                    user["email"],
                                                    f"CryptoEdge: {ticker} {p['pred_direction'].upper()} ({conf}%)",
                                                    ticker, p["pred_direction"], conf, delta_str,
                                                )
                                                db.log_alert(user["id"], ticker, "email", f"{p['pred_direction']} {conf}%", p["confidence"])
                            except Exception as e:
                                print(f"  Email alert error: {e}")

                print(f"  Refreshed {ticker} windows at {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
            except Exception as e:
                print(f"  Refresh error {ticker}: {e}")


last_sus_scan: dict = {}  # cached suspicious scan results
last_sus_scan_time: float = 0

async def suspicious_trade_monitor():
    """Periodically scan for suspicious trades and push alerts for new ones."""
    global last_sus_scan, last_sus_scan_time, connected_ws
    seen_trade_keys: set = set()

    # Wait for server to be ready
    await asyncio.sleep(30)

    while True:
        try:
            from suspicious_trades import run_scanner
            result = await asyncio.to_thread(run_scanner)
            if result and result.get("suspicious_trades"):
                last_sus_scan = result
                last_sus_scan_time = time.time()

                # Check for new trades we haven't seen
                for t in result["suspicious_trades"]:
                    key = f'{t.get("wallet","")[:12]}_{t.get("usd_value",0)}_{t.get("title","")[:20]}'
                    pot_profit = t.get("potential_profit", 0)
                    if key not in seen_trade_keys and (pot_profit >= 5000 or t.get("score", 0) >= 50):
                        seen_trade_keys.add(key)
                        odds_str = t.get("odds_str", f'{t.get("price",0):.0%}')
                        # Push alert to connected clients
                        if connected_ws:
                            alert = json.dumps({
                                "type": "alert",
                                "data": {
                                    "ticker": "SUS",
                                    "direction": "suspicious",
                                    "confidence": t.get("score", 0),
                                    "delta": f'${t.get("usd_value",0):,.0f} at {odds_str} → ${pot_profit:,.0f} profit on {t.get("title","")[:35]}',
                                    "time": t.get("time_str", ""),
                                },
                            })
                            for ws in list(connected_ws):
                                try:
                                    await ws.send_text(alert)
                                except Exception:
                                    connected_ws.discard(ws)

                # Keep set bounded -- clear entirely since set is unordered
                if len(seen_trade_keys) > 5000:
                    # Keep most recent entries instead of clearing all
                    to_remove = list(seen_trade_keys)[:2500]
                    for k in to_remove:
                        seen_trade_keys.discard(k)

                print(f"  [SUS] Scan complete: {len(result['suspicious_trades'])} flagged trades")
        except Exception as e:
            print(f"  [SUS] Scanner error: {e}")

        await asyncio.sleep(1800)  # re-scan every 30 minutes


# ─── News-Trade Correlation Monitor ─────────────────────────────────

last_news_trade_scan: dict = {}
last_news_trade_time: float = 0


async def news_trade_monitor():
    """Scan news for insider-trading stories every 20 minutes.
    Cross-references with Polymarket, persists to DB, pushes WebSocket
    alerts for high-score items, and sends email to watchlist subscribers.
    """
    global last_news_trade_scan, last_news_trade_time, connected_ws

    # Wait for server to be ready
    await asyncio.sleep(45)

    while True:
        try:
            from news_trade_scanner import run_news_trade_scan
            # Pass existing suspicious trades so scanner doesn't re-run the full scan
            sus = last_sus_scan.get("suspicious_trades", []) if last_sus_scan else None
            result = await asyncio.to_thread(run_news_trade_scan, sus)

            if result and result.get("alerts"):
                last_news_trade_scan = result
                last_news_trade_time = time.time()

                # Persist alerts to DB
                for alert in result["alerts"]:
                    try:
                        db.upsert_news_alert(alert)
                    except Exception as e:
                        print(f"  [NEWS-TRADE] DB error: {e}")

                # Find new high-score alerts to push
                new_alerts = db.get_unnotified_alerts(min_score=30)
                for alert in new_alerts:
                    # Push WebSocket notification
                    if connected_ws:
                        ws_msg = json.dumps({
                            "type": "alert",
                            "data": {
                                "ticker": "NEWS",
                                "direction": "news_trade",
                                "confidence": alert["score"],
                                "delta": f'[{alert["source"]}] {alert["title"][:60]}',
                                "time": alert.get("scanned_at", ""),
                                "alert_id": alert["id"],
                                "link": alert.get("link", ""),
                                "related_markets": alert.get("related_markets", []),
                            },
                        })
                        for ws in list(connected_ws):
                            try:
                                await ws.send_text(ws_msg)
                            except Exception:
                                connected_ws.discard(ws)

                    # Email watchlist subscribers
                    watchers = db.get_watchlist_users_for_alert(alert["id"])
                    for w in watchers:
                        if w.get("notify_email") and w.get("email"):
                            try:
                                from email_alerts import send_news_trade_alert
                                await asyncio.to_thread(
                                    send_news_trade_alert, w["email"], dict(alert)
                                )
                            except Exception:
                                pass

                    db.mark_alert_notified(alert["id"])

                print(f"  [NEWS-TRADE] Scan complete: {len(result['alerts'])} alerts, {len(new_alerts)} new pushes")
        except Exception as e:
            print(f"  [NEWS-TRADE] Monitor error: {e}")

        await asyncio.sleep(1200)  # re-scan every 20 minutes


def serialize_asset(ticker):
    """Convert asset state to JSON-safe dict."""
    if ticker not in asset_state:
        return {}
    st = asset_state[ticker]
    s = st["summary"]
    vol = st["volatility"]
    vel = st["velocity"]
    bt = st["backtest"]

    preds_out = []
    for p in (st["predictions"] or []):
        preds_out.append({
            "window_start": p["window_start"].isoformat() if hasattr(p["window_start"], "isoformat") else str(p["window_start"]),
            "pred_direction": p["pred_direction"],
            "pred_end_delta": p["pred_end_delta"],
            "pred_prob_positive": p["pred_prob_positive"],
            "confidence": p["confidence"],
            "is_current": p["is_current"],
            "actual_end_delta": p["actual_end_delta"],
        })

    # Last 20 windows for the API (not all 8600+)
    recent_windows = []
    for w in st["windows"][-20:]:
        recent_windows.append({
            "start": w["start"].isoformat(),
            "baseline": w["baseline"],
            "end_delta": w["end_delta"],
            "max_positive": w["max_positive"],
            "max_negative": w["max_negative"],
            "last_cross_sec": w["last_cross_sec"],
            "last_cross_direction": w["last_cross_direction"],
            "rsi": w["rsi"],
            "crossings": w["crossings"],
            "avg_pos_magnitude": w["avg_pos_magnitude"],
            "avg_neg_magnitude": w["avg_neg_magnitude"],
        })

    return {
        "ticker": ticker,
        "name": ASSETS[ticker]["name"],
        "price": live_prices.get(ticker, 0),
        "summary": s,
        "volatility": vol,
        "velocity": vel,
        "backtest": {
            "dir_acc": bt["dir_acc"],
            "hc_acc": bt["hc_acc"],
            "hc_count": bt["hc_count"],
            "mae": bt["mae"],
            "total": bt["total"],
        } if bt else None,
        "predictions": preds_out,
        "recent_windows": recent_windows,
    }


# ─── Auth Redirects ──────────────────────────────────────────────────
# All auth is handled by the gateway. These just redirect.


@app.get("/login")
async def login_page():
    return RedirectResponse("https://habbig.com/login", status_code=302)


@app.get("/signup")
async def signup_page():
    return RedirectResponse("https://habbig.com/signup", status_code=302)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("https://habbig.com/logout", status_code=302)
    resp.delete_cookie("session")
    return resp


# ─── REST Endpoints ──────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    """Serve the live crypto dashboard."""
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    if not asset_state:
        return HTMLResponse("<h1>Loading... refresh in 30s</h1>")
    # Generate and serve the dashboard with live JS injected
    all_results = {}
    for ticker in asset_state:
        st = asset_state[ticker]
        # Generate chart data from raw kline data
        raw_data = st.get("data", [])
        chart_24h = []
        chart_7d = []
        if raw_data:
            now_ms = raw_data[-1][0]
            day_ms = 24 * 3600 * 1000
            week_ms = 7 * day_ms
            for ts, price in raw_data:
                if ts >= now_ms - day_ms:
                    chart_24h.append({"t": ts // 1000, "v": round(price, 2)})
            if len(chart_24h) > 3000:
                step = len(chart_24h) // 2880
                chart_24h = chart_24h[::step]
            for ts, price in raw_data:
                if ts >= now_ms - week_ms:
                    chart_7d.append({"t": ts // 1000, "v": round(price, 2)})
            if len(chart_7d) > 2016:
                step = len(chart_7d) // 2016
                chart_7d = chart_7d[::step]
        all_results[ticker] = {
            "windows": st["windows"],
            "summary": st["summary"],
            "volatility": st["volatility"],
            "velocity": st["velocity"],
            "backtest": st["backtest"],
            "predictions": st["predictions"],
            "model_info": st.get("model_info"),
            "start_dt": st["start_dt"],
            "end_dt": st["end_dt"],
            "chart_24h": chart_24h,
            "chart_7d": chart_7d,
        }
    # Suspicious trades: premium only — use the background monitor's cache
    # instead of re-running the scanner on every page load.
    sus_data = None
    if _is_premium(request) and last_sus_scan:
        sus_data = dict(last_sus_scan)
        if sus_data.get("suspicious_trades"):
            sus_data["suspicious_trades"] = [
                t for t in sus_data["suspicious_trades"]
                if t.get("potential_profit", 0) >= 1000 or t.get("score", 0) >= 40
            ]
    html = generate_dashboard(all_results, suspicious_data=sus_data)

    # Inject nav bar with user info
    user = _get_session_user(request)
    user_name = html_mod.escape((user.get("display_name") or user.get("email", "")) if user else "")
    tier_label = html_mod.escape(user.get("tier", "free").upper() if user else "FREE")
    tier_color = "var(--green)" if tier_label in ("PREMIUM","ADMIN") else "var(--muted)"
    nav_html = f"""
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding:8px 0;border-bottom:1px solid var(--border);">
  <div style="display:flex;gap:12px;align-items:center;font-size:0.8em;">
    <span style="color:var(--muted);">{user_name}</span>
    <span style="color:{tier_color};font-weight:600;">{tier_label}</span>
  </div>
  <div style="display:flex;gap:12px;align-items:center;font-size:0.8em;">
    <a href="/kalshi" style="color:var(--muted);text-decoration:none;">Kalshi</a>
    <a href="/trade" style="color:var(--blue);text-decoration:none;font-weight:600;">Trade</a>
    <a href="/accuracy" style="color:var(--muted);text-decoration:none;">Accuracy</a>
    <a href="/settings" style="color:var(--muted);text-decoration:none;">Settings</a>
    <a href="/logout" style="color:var(--red);text-decoration:none;font-weight:600;">Logout</a>
  </div>
</div>
"""
    html = html.replace("<body>", "<body>" + nav_html, 1)

    # Inject live-update WebSocket script + browser notifications
    ws_script = """
<script>
(function() {
  // Request notification permission
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  function notify(title, body, tag) {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(title, { body: body, icon: '/favicon.ico', tag: tag, renotify: true });
    }
  }

  // ── Incremental DOM update helpers ──

  function renderPredCard(p) {
    var isCur = p.is_current;
    var border = isCur ? 'var(--green)' : 'var(--border)';
    var label = isCur ? 'UPCOMING' : 'COMPLETED';
    var labelColor = isCur ? 'var(--green)' : 'var(--muted)';
    var dirClass = p.pred_direction === 'positive' ? 'positive' : 'negative';
    var prob = p.pred_prob_positive;
    var probStr = prob >= 0.5 ? (prob * 100).toFixed(0) + '%' : ((1 - prob) * 100).toFixed(0) + '%';
    var conf = (p.confidence * 100).toFixed(0) + '%';
    var delta = (p.pred_end_delta >= 0 ? '+' : '') + p.pred_end_delta.toFixed(2);
    var timeStr = p.window_start ? p.window_start.replace(/T/, ' ').substring(11, 16) : '\\u2014';

    var actualStr = '';
    if (p.actual_end_delta !== null && p.actual_end_delta !== undefined) {
      var ac = p.actual_end_delta;
      var acClass = ac >= 0 ? 'positive' : 'negative';
      var acStr = (ac >= 0 ? '+' : '') + ac.toFixed(2);
      actualStr = '<div class="detail">Actual: <span class="' + acClass + '">$' + acStr + '</span></div>';
    }

    return '<div class="pred-card" style="border-color:' + border + ';">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">' +
        '<span style="font-size:1.1em;font-weight:700;">' + timeStr + ' UTC</span>' +
        '<span style="font-size:0.7em;color:' + labelColor + ';font-weight:600;letter-spacing:0.05em;">' + label + '</span>' +
      '</div>' +
      '<div style="display:flex;gap:16px;flex-wrap:wrap;">' +
        '<div><span class="mini-label">Direction</span><br><span class="value-sm ' + dirClass + '">' + p.pred_direction.toUpperCase() + '</span></div>' +
        '<div><span class="mini-label">Delta</span><br><span class="value-sm">$' + delta + '</span></div>' +
        '<div><span class="mini-label">Prob</span><br><span class="value-sm">' + probStr + '</span></div>' +
        '<div><span class="mini-label">Confidence</span><br><span class="value-sm">' + conf + '</span></div>' +
      '</div>' +
      actualStr +
    '</div>';
  }

  function updatePredictions(ticker, predictions) {
    var grid = document.getElementById('pred-grid-' + ticker);
    if (!grid || !predictions) return;
    if (predictions.length === 0) {
      grid.innerHTML = '<div class="pred-card" style="border-color:var(--yellow);"><div style="padding:12px;text-align:center;color:var(--yellow);font-weight:600;">Models training on GPU... predictions will appear shortly.</div></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < predictions.length; i++) {
      html += renderPredCard(predictions[i]);
    }
    grid.innerHTML = html;
  }

  function updateWindowRows(ticker, windows) {
    // Update the window table if the details element is open
    var tab = document.getElementById('tab-' + ticker);
    if (!tab) return;
    var tbody = tab.querySelector('details tbody');
    if (!tbody) return;
    // Only update if details is open (user is viewing)
    var details = tab.querySelector('details');
    if (!details || !details.open) return;

    var html = '';
    for (var i = 0; i < windows.length; i++) {
      var w = windows[i];
      var ec = w.end_delta >= 0 ? 'positive' : 'negative';
      var crossS = w.last_cross_sec ? (w.last_cross_sec.toFixed(0) + 's\\u2192' + (w.last_cross_direction || '').substring(0, 3)) : '\\u2014';
      var rsiClass = w.rsi > 70 ? 'negative' : (w.rsi < 30 ? 'positive' : '');
      var startStr = w.start ? w.start.substring(5, 16).replace('T', ' ') : '';
      html += '<tr>' +
        '<td>' + startStr + '</td>' +
        '<td>$' + w.baseline.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) + '</td>' +
        '<td class="' + ec + '">$' + (w.end_delta >= 0 ? '+' : '') + w.end_delta.toFixed(2) + '</td>' +
        '<td class="positive">$+' + w.max_positive.toFixed(2) + '</td>' +
        '<td class="negative">$' + w.max_negative.toFixed(2) + '</td>' +
        '<td>$' + (w.avg_pos_magnitude >= 0 ? '+' : '') + w.avg_pos_magnitude.toFixed(2) + ' / $' + w.avg_neg_magnitude.toFixed(2) + '</td>' +
        '<td>' + crossS + '</td>' +
        '<td class="' + rsiClass + '">' + w.rsi.toFixed(0) + '</td>' +
        '<td>' + w.crossings + '</td>' +
      '</tr>';
    }
    tbody.innerHTML = html;
  }

  function applyAssetUpdate(ticker, data) {
    // Update live price
    var priceEl = document.getElementById('live-price-' + ticker);
    if (priceEl && data.price) {
      priceEl.textContent = '$' + data.price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
    }
    // Update predictions
    if (data.predictions) {
      updatePredictions(ticker, data.predictions);
    }
    // Update recent window rows
    if (data.recent_windows) {
      updateWindowRows(ticker, data.recent_windows);
    }
    // Update timestamp
    var ts = document.getElementById('last-update');
    if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
  }

  // ── WebSocket with auto-reconnect (no page reload) ──

  var wsReconnectDelay = 1000;
  var wsMaxDelay = 30000;

  function connectWS() {
    var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    var ws = new WebSocket(proto + '//' + window.location.host + '/ws');

    ws.onopen = function() {
      wsReconnectDelay = 1000;  // reset on successful connect
      var ts = document.getElementById('last-update');
      if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
    };

    ws.onmessage = function(e) {
      var msg = JSON.parse(e.data);

      if (msg.type === 'price_update') {
        for (var ticker in msg.data) {
          var d = msg.data[ticker];
          var el = document.getElementById('live-price-' + ticker);
          if (el) el.textContent = '$' + d.price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2});
        }
        var ts = document.getElementById('last-update');
        if (ts) ts.textContent = 'Live \\u2022 ' + new Date().toLocaleTimeString();
      }

      if (msg.type === 'window_update') {
        // Incremental update: apply new data to DOM without page reload
        if (msg.ticker && msg.data) {
          applyAssetUpdate(msg.ticker, msg.data);
          showToast(msg.ticker + ': new window data received');
        }
      }

      if (msg.type === 'init') {
        // Apply initial state from WS connection
        if (msg.data) {
          for (var t in msg.data) {
            applyAssetUpdate(t, msg.data[t]);
          }
        }
      }

      if (msg.type === 'alert') {
        var a = msg.data;
        notify('CryptoEdge Alert: ' + a.ticker,
               a.direction.toUpperCase() + ' signal (' + a.confidence + '% confidence) | Delta: $' + a.delta,
               'signal-' + a.ticker);
        showToast(a.ticker + ': ' + a.direction.toUpperCase() + ' (' + a.confidence + '% conf)');
      }
    };

    ws.onclose = function() {
      // Reconnect with exponential backoff instead of full page reload
      var ts = document.getElementById('last-update');
      if (ts) ts.textContent = 'Reconnecting...';
      setTimeout(function() {
        connectWS();
      }, wsReconnectDelay);
      wsReconnectDelay = Math.min(wsReconnectDelay * 2, wsMaxDelay);
    };

    ws.onerror = function() {
      ws.close();
    };
  }

  connectWS();

  // ── Periodic data refresh via fetch (no page reload) ──

  setInterval(function() {
    fetch('/api/state', { credentials: 'same-origin' })
      .then(function(r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function(data) {
        if (!data) return;
        for (var ticker in data) {
          applyAssetUpdate(ticker, data[ticker]);
        }
      })
      .catch(function() {
        // Silently ignore fetch errors; WS is the primary channel
      });
  }, 60000);

  // In-page toast notifications
  function showToast(msg) {
    var toast = document.createElement('div');
    toast.style.cssText = 'position:fixed;top:16px;right:16px;background:#1c2333;border:1px solid #58a6ff;' +
      'color:#e6edf3;padding:12px 20px;border-radius:8px;font-size:0.9em;z-index:9999;animation:fadeIn 0.3s;' +
      'box-shadow:0 4px 12px rgba(0,0,0,0.4);max-width:400px;';
    toast.textContent = '\\u26A1 ' + msg;
    document.body.appendChild(toast);
    setTimeout(function() { toast.style.opacity = '0'; toast.style.transition = 'opacity 0.5s'; }, 4000);
    setTimeout(function() { toast.remove(); }, 4500);
  }
})();
</script>
"""
    html = html.replace("</body>", ws_script + "</body>")
    return HTMLResponse(html)


# ─── Internal data endpoints (used by dashboard JS only, not public API) ───

def _get_bot_signals():
    """Compute trading signals for internal use by trading bots on localhost."""
    signals = {}
    for ticker in asset_state:
        st = asset_state[ticker]
        vel = st.get("velocity", {})
        vol = st.get("volatility", {})
        windows = st.get("windows", [])
        last_window = windows[-1] if windows else None

        recent_wins = windows[-200:] if len(windows) >= 200 else windows
        pos_windows = [w for w in recent_wins if w["end_delta"] >= 0]
        neg_windows = [w for w in recent_wins if w["end_delta"] < 0]

        avg_pos_delta = float(np.mean([w["end_delta"] for w in pos_windows])) if pos_windows else 0
        avg_neg_delta = float(np.mean([w["end_delta"] for w in neg_windows])) if neg_windows else 0
        avg_max_up = float(np.mean([w["max_positive"] for w in recent_wins])) if recent_wins else 0
        avg_max_down = float(np.mean([w["max_negative"] for w in recent_wins])) if recent_wins else 0
        win_rate = len(pos_windows) / len(recent_wins) * 100 if recent_wins else 50
        avg_rsi_when_up = float(np.mean([w["rsi"] for w in pos_windows])) if pos_windows else 50
        avg_rsi_when_down = float(np.mean([w["rsi"] for w in neg_windows])) if neg_windows else 50
        avg_crossings_winners = float(np.mean([w["crossings"] for w in pos_windows])) if pos_windows else 3
        avg_crossings_losers = float(np.mean([w["crossings"] for w in neg_windows])) if neg_windows else 3

        signal = {
            "ticker": ticker, "price": live_prices.get(ticker, 0),
            "volatility_label": vol.get("label", "UNKNOWN"),
            "gain_loss_ratio": vel.get("gain_loss_ratio", 0),
            "momentum_decay": vel.get("momentum_decay_ratio", 1),
            "avg_velocity_after_cross_pos": vel.get("avg_velocity_after_cross_pos", 0),
            "avg_velocity_after_cross_neg": vel.get("avg_velocity_after_cross_neg", 0),
            "best_entry_sec": vel.get("best_entry_sec", 0),
            "pct_seconds_gaining": vel.get("pct_seconds_gaining", 50),
            "avg_time_to_peak": vel.get("avg_time_to_peak_sec", 150),
            "avg_time_to_trough": vel.get("avg_time_to_trough_sec", 150),
            "avg_gain_per_sec": vel.get("avg_gain_per_sec", 0),
            "avg_loss_per_sec": vel.get("avg_loss_per_sec", 0),
            "hist_avg_pos_delta": avg_pos_delta, "hist_avg_neg_delta": avg_neg_delta,
            "hist_avg_max_up": avg_max_up, "hist_avg_max_down": avg_max_down,
            "hist_win_rate": win_rate,
            "hist_avg_rsi_when_up": avg_rsi_when_up, "hist_avg_rsi_when_down": avg_rsi_when_down,
            "hist_avg_crossings_winners": avg_crossings_winners, "hist_avg_crossings_losers": avg_crossings_losers,
        }
        if last_window:
            signal.update({
                "last_cross_sec": last_window["last_cross_sec"],
                "last_cross_direction": last_window["last_cross_direction"],
                "rsi": last_window["rsi"], "crossings": last_window["crossings"],
                "current_delta": last_window["end_delta"],
                "current_max_up": last_window["max_positive"],
                "current_max_down": last_window["max_negative"],
                "current_avg_delta": last_window["avg_delta"],
                "current_positive_pct": last_window["positive_pct"],
                "window_baseline": last_window["baseline"],
            })
        signals[ticker] = signal
    return signals


@app.get("/api/state")
async def api_state(request: Request):
    """Return current asset state for incremental dashboard refresh (no page reload)."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {ticker: serialize_asset(ticker) for ticker in asset_state}


@app.get("/_internal/bot/signals")
async def internal_bot_signals(request: Request):
    """Localhost-only signals endpoint for trading bots."""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return _get_bot_signals()


# ─── Bot Dashboard ────────────────────────────────────────────────────

@app.get("/_internal/bot/status")
async def get_bot_status(request: Request):
    """Internal: bot state for the bot dashboard page. Auth checked via session."""
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trade_file = Path(__file__).parent / "trades.json"
    log_file = Path(__file__).parent / "bot_activity.log"
    result = {"running": False, "balance": 0, "total_pnl": 0, "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "peak_balance": 0, "max_drawdown": 0, "consecutive_losses": 0, "trades": [], "log": [], "positions": []}
    if trade_file.exists():
        try:
            with open(trade_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return result
        result["running"] = True
        result["balance"] = data.get("balance", 0)
        result["total_trades"] = data.get("total_trades", 0)
        result["winning_trades"] = data.get("winning_trades", 0)
        result["losing_trades"] = data.get("losing_trades", 0)
        result["total_pnl"] = data.get("total_pnl", 0)
        result["peak_balance"] = data.get("peak_balance", 0)
        result["max_drawdown"] = data.get("max_drawdown", 0)
        result["consecutive_losses"] = data.get("consecutive_losses", 0)
        result["positions"] = data.get("positions", [])
        result["trades"] = data.get("closed_trades", [])[-50:]  # last 50
    if log_file.exists():
        with open(log_file) as f:
            lines = f.readlines()
        result["log"] = [l.strip() for l in lines[-100:]]  # last 100 lines
    return result


@app.get("/bot", response_class=HTMLResponse)
async def bot_dashboard(request: Request):
    """Self-contained bot monitoring dashboard."""
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Monitor</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a1a; color:#e0e0e0; font-family:'SF Mono',Monaco,monospace; padding:16px; }
  h1 { color:#00d4aa; font-size:1.5em; margin-bottom:8px; }
  .subtitle { color:#888; font-size:0.8em; margin-bottom:16px; }
  .live-dot { display:inline-block; width:8px; height:8px; background:#00ff88; border-radius:50%;
    animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#141428; border:1px solid #2a2a4a; border-radius:10px; padding:14px; }
  .card .label { color:#888; font-size:0.7em; text-transform:uppercase; }
  .card .value { font-size:1.4em; font-weight:700; margin-top:4px; }
  .positive { color:#00ff88; }
  .negative { color:#ff4466; }
  .neutral { color:#ffaa00; }
  .section { margin-bottom:20px; }
  .section h2 { color:#aaa; font-size:0.9em; margin-bottom:8px; border-bottom:1px solid #2a2a4a; padding-bottom:4px; }
  table { width:100%; border-collapse:collapse; font-size:0.8em; }
  th { background:#1a1a2e; color:#888; text-align:left; padding:8px; }
  td { padding:8px; border-bottom:1px solid #1a1a2e; }
  .log-box { background:#0d0d1a; border:1px solid #2a2a4a; border-radius:8px; padding:12px;
    max-height:400px; overflow-y:auto; font-size:0.75em; line-height:1.6; }
  .log-line { border-bottom:1px solid #111; padding:2px 0; }
  .log-line.trade { color:#00d4aa; font-weight:600; }
  .log-line.loss { color:#ff4466; }
  .log-line.warn { color:#ffaa00; }
  .positions-empty { color:#666; font-style:italic; padding:12px; }
  @media(max-width:600px) { .grid { grid-template-columns:1fr 1fr; } }
</style>
</head><body>
<h1>Trading Bot Monitor</h1>
<p class="subtitle"><span class="live-dot"></span> <span id="status">Loading...</span> &middot; Auto-refresh 5s</p>

<div class="grid" id="stats"></div>

<div class="section">
  <h2>Open Positions</h2>
  <div id="positions"><p class="positions-empty">No open positions</p></div>
</div>

<div class="section">
  <h2>Recent Trades</h2>
  <div id="trades"></div>
</div>

<div class="section">
  <h2>Activity Log</h2>
  <div class="log-box" id="log"></div>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/_internal/bot/status');
    const d = await r.json();

    const wr = d.total_trades > 0 ? (d.winning_trades / d.total_trades * 100).toFixed(1) : '0.0';
    const pf = d.losing_trades > 0 && d.winning_trades > 0
      ? (d.trades.filter(t=>t.pnl>0).reduce((s,t)=>s+t.pnl,0) /
         Math.abs(d.trades.filter(t=>t.pnl<=0).reduce((s,t)=>s+t.pnl,0))).toFixed(2)
      : '∞';
    const dd = d.peak_balance > 0 ? ((d.peak_balance - d.balance) / d.peak_balance * 100).toFixed(2) : '0.00';

    document.getElementById('status').textContent = d.running
      ? 'Running · Balance: $' + d.balance.toLocaleString(undefined,{minimumFractionDigits:2})
      : 'Bot offline';

    document.getElementById('stats').innerHTML = `
      <div class="card"><div class="label">Balance</div><div class="value">$${d.balance.toLocaleString(undefined,{minimumFractionDigits:2})}</div></div>
      <div class="card"><div class="label">Total PnL</div><div class="value ${d.total_pnl>=0?'positive':'negative'}">$${d.total_pnl>=0?'+':''}${d.total_pnl.toFixed(2)}</div></div>
      <div class="card"><div class="label">Trades</div><div class="value">${d.total_trades}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value ${parseFloat(wr)>=50?'positive':'negative'}">${wr}%</div></div>
      <div class="card"><div class="label">W / L</div><div class="value"><span class="positive">${d.winning_trades}</span> / <span class="negative">${d.losing_trades}</span></div></div>
      <div class="card"><div class="label">Profit Factor</div><div class="value">${pf}</div></div>
      <div class="card"><div class="label">Drawdown</div><div class="value ${parseFloat(dd)>3?'negative':'neutral'}">${dd}%</div></div>
      <div class="card"><div class="label">Consec Losses</div><div class="value ${d.consecutive_losses>=3?'negative':''}">${d.consecutive_losses}</div></div>
    `;

    // Positions
    if (d.positions && d.positions.length > 0) {
      let ph = '<table><tr><th>Asset</th><th>Dir</th><th>Entry</th><th>Size</th><th>Stop</th><th>Score</th></tr>';
      d.positions.forEach(p => {
        ph += '<tr><td>'+p.ticker+'</td><td>'+p.direction.toUpperCase()+'</td><td>$'+parseFloat(p.entry_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>$'+parseFloat(p.bet_amount).toFixed(2)+'</td><td>$'+parseFloat(p.trailing_stop_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>'+p.score+'</td></tr>';
      });
      ph += '</table>';
      document.getElementById('positions').innerHTML = ph;
    } else {
      document.getElementById('positions').innerHTML = '<p class="positions-empty">No open positions</p>';
    }

    // Trades (newest first)
    const trades = (d.trades || []).reverse().slice(0, 20);
    if (trades.length > 0) {
      let th = '<table><tr><th>Asset</th><th>Dir</th><th>PnL</th><th>%</th><th>Entry</th><th>Exit</th><th>Reason</th></tr>';
      trades.forEach(t => {
        const cls = t.pnl >= 0 ? 'positive' : 'negative';
        th += '<tr><td>'+t.ticker+'</td><td>'+t.direction.toUpperCase()+'</td><td class="'+cls+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td><td class="'+cls+'">'+(t.pnl_pct>=0?'+':'')+t.pnl_pct.toFixed(2)+'%</td><td>$'+parseFloat(t.entry_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>$'+parseFloat(t.exit_price).toLocaleString(undefined,{minimumFractionDigits:2})+'</td><td>'+t.exit_reason+'</td></tr>';
      });
      th += '</table>';
      document.getElementById('trades').innerHTML = th;
    } else {
      document.getElementById('trades').innerHTML = '<p class="positions-empty">No trades yet</p>';
    }

    // Log (newest first)
    function escapeHtml(s) {
      const d = document.createElement('div');
      d.appendChild(document.createTextNode(s));
      return d.innerHTML;
    }
    const lines = (d.log || []).reverse();
    document.getElementById('log').innerHTML = lines.map(l => {
      let cls = 'log-line';
      if (l.includes('OPEN') || l.includes('WIN')) cls += ' trade';
      if (l.includes('LOSS')) cls += ' loss';
      if (l.includes('COOLDOWN') || l.includes('paused')) cls += ' warn';
      return '<div class="'+cls+'">'+escapeHtml(l)+'</div>';
    }).join('');
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Polymarket Bot Dashboard ─────────────────────────────────────────

@app.get("/_internal/polybot/status")
async def get_polybot_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    trade_file = Path(__file__).parent / "poly_trades.json"
    log_file = Path(__file__).parent / "poly_bot_activity.log"
    result = {"running": False, "balance": 0, "total_pnl": 0, "total_trades": 0,
              "wins": 0, "losses": 0, "peak_balance": 0, "pending": None,
              "trades": [], "log": []}
    if trade_file.exists():
        try:
            with open(trade_file) as f:
                data = json.load(f)
            result["running"] = True
            result["balance"] = data.get("balance", 0)
            result["total_trades"] = data.get("total_trades", 0)
            result["wins"] = data.get("wins", 0)
            result["losses"] = data.get("losses", 0)
            result["total_pnl"] = data.get("total_pnl", 0)
            result["peak_balance"] = data.get("peak_balance", 0)
            result["pending"] = data.get("pending")
            result["trades"] = data.get("trades", [])[-50:]
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            print(f"  [BOT STATUS] Error reading state: {e}")
    if log_file.exists():
        try:
            with open(log_file) as f:
                result["log"] = [l.strip() for l in f.readlines()[-100:]]
        except OSError as e:
            print(f"  [BOT STATUS] Error reading log: {e}")
    return result


@app.get("/polybot", response_class=HTMLResponse)
async def polybot_dashboard(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Multi-Coin Bot</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a1a; color:#e0e0e0; font-family:'SF Mono',Monaco,monospace; padding:16px; }
  h1 { color:#f7931a; font-size:1.5em; margin-bottom:8px; }
  .subtitle { color:#888; font-size:0.8em; margin-bottom:16px; }
  .live-dot { display:inline-block; width:8px; height:8px; background:#f7931a; border-radius:50%; animation:pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-bottom:20px; }
  .card { background:#141428; border:1px solid #2a2a4a; border-radius:10px; padding:14px; }
  .card .label { color:#888; font-size:0.7em; text-transform:uppercase; }
  .card .value { font-size:1.3em; font-weight:700; margin-top:4px; }
  .positive { color:#00ff88; }
  .negative { color:#ff4466; }
  .pending-box { background:#1a1a2e; border:2px solid #f7931a; border-radius:10px; padding:16px; margin-bottom:20px; }
  .pending-box h3 { color:#f7931a; margin-bottom:8px; }
  .section { margin-bottom:20px; }
  .section h2 { color:#aaa; font-size:0.9em; margin-bottom:8px; border-bottom:1px solid #2a2a4a; padding-bottom:4px; }
  table { width:100%; border-collapse:collapse; font-size:0.8em; }
  th { background:#1a1a2e; color:#888; text-align:left; padding:8px; }
  td { padding:8px; border-bottom:1px solid #1a1a2e; }
  .log-box { background:#0d0d1a; border:1px solid #2a2a4a; border-radius:8px; padding:12px; max-height:400px; overflow-y:auto; font-size:0.75em; line-height:1.6; }
  .log-line { border-bottom:1px solid #111; padding:2px 0; }
  .log-line.win { color:#00ff88; font-weight:600; }
  .log-line.loss { color:#ff4466; }
  .log-line.bet { color:#f7931a; }
  .empty { color:#666; font-style:italic; padding:12px; }
</style>
</head><body>
<h1>Polymarket Multi-Coin 5-Min Bot</h1>
<p class="subtitle"><span class="live-dot"></span> <span id="status">Loading...</span> &middot; $100 per trade &middot; BTC ETH SOL DOGE XRP BNB &middot; Auto-refresh 5s</p>
<div class="grid" id="stats"></div>
<div id="pending"></div>
<div class="section"><h2>Recent Trades</h2><div id="trades"></div></div>
<div class="section"><h2>Activity Log</h2><div class="log-box" id="log"></div></div>
<script>
async function refresh() {
  try {
    const r = await fetch('/_internal/polybot/status');
    const d = await r.json();
    const wr = d.total_trades > 0 ? (d.wins/d.total_trades*100).toFixed(1) : '0.0';
    const avgWin = d.trades.filter(t=>t.pnl>0);
    const avgLoss = d.trades.filter(t=>t.pnl<=0);
    const avgW = avgWin.length > 0 ? (avgWin.reduce((s,t)=>s+t.pnl,0)/avgWin.length).toFixed(2) : '0';
    const avgL = avgLoss.length > 0 ? (avgLoss.reduce((s,t)=>s+t.pnl,0)/avgLoss.length).toFixed(2) : '0';

    // pending can be a dict of coins or a single object (old format)
    const pending = d.pending || {};
    const pendingEntries = (typeof pending === 'object' && !pending.side)
      ? Object.entries(pending).filter(([k,v]) => v !== null)
      : (pending && pending.side ? [['btc', pending]] : []);
    const liveBets = pendingEntries.length;

    document.getElementById('status').textContent = d.running
      ? (liveBets > 0 ? liveBets + ' LIVE BET' + (liveBets>1?'S':'') + ' — waiting for resolution' : 'Scanning for edge...')
      : 'Bot offline';
    document.getElementById('stats').innerHTML = `
      <div class="card"><div class="label">Balance</div><div class="value">$${(d.balance||0).toLocaleString(undefined,{minimumFractionDigits:2})}</div></div>
      <div class="card"><div class="label">Total PnL</div><div class="value ${(d.total_pnl||0)>=0?'positive':'negative'}">$${(d.total_pnl||0)>=0?'+':''}${(d.total_pnl||0).toFixed(2)}</div></div>
      <div class="card"><div class="label">Trades</div><div class="value">${d.total_trades||0}</div></div>
      <div class="card"><div class="label">Win Rate</div><div class="value ${parseFloat(wr)>=50?'positive':'negative'}">${wr}%</div></div>
      <div class="card"><div class="label">W / L</div><div class="value"><span class="positive">${d.wins||0}</span> / <span class="negative">${d.losses||0}</span></div></div>
      <div class="card"><div class="label">Avg Win</div><div class="value positive">$${avgW}</div></div>
      <div class="card"><div class="label">Avg Loss</div><div class="value negative">$${avgL}</div></div>
      <div class="card"><div class="label">Active Bets</div><div class="value" style="color:#f7931a">${liveBets} / 6</div></div>
    `;
    if (liveBets > 0) {
      let ph = '';
      pendingEntries.forEach(([coin, p]) => {
        const potWin = (p.shares * 1.0 - p.amount).toFixed(2);
        ph += `<div class="pending-box">
          <h3>LIVE BET — ${coin.toUpperCase()}</h3>
          <p><strong>${p.side.toUpperCase()}</strong> @ $${p.buy_price.toFixed(3)} | ${p.shares.toFixed(1)} shares | Edge: ${(p.edge*100).toFixed(1)}%</p>
          <p>Potential: <span class="positive">+$${potWin}</span> / <span class="negative">-$${p.amount}</span></p>
          <p style="color:#888;font-size:0.8em">${p.title}</p>
        </div>`;
      });
      document.getElementById('pending').innerHTML = ph;
    } else {
      document.getElementById('pending').innerHTML = '';
    }
    const trades = (d.trades||[]).reverse().slice(0,30);
    if (trades.length > 0) {
      let h = '<table><tr><th>Coin</th><th>Side</th><th>Price</th><th>Edge</th><th>Result</th><th>PnL</th></tr>';
      trades.forEach(t => {
        const cls = t.pnl >= 0 ? 'positive' : 'negative';
        const coin = (t.coin || 'btc').toUpperCase();
        h += '<tr><td>'+coin+'</td><td>'+t.side.toUpperCase()+'</td><td>$'+t.buy_price.toFixed(3)+'</td><td>'+(t.edge*100).toFixed(1)+'%</td><td class="'+cls+'">'+t.result+'</td><td class="'+cls+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td></tr>';
      });
      h += '</table>';
      document.getElementById('trades').innerHTML = h;
    } else {
      document.getElementById('trades').innerHTML = '<p class="empty">No trades yet. Bot is waiting for mispriced markets.</p>';
    }
    function escapeHtml(s) {
      const d = document.createElement('div');
      d.appendChild(document.createTextNode(s));
      return d.innerHTML;
    }
    const lines = (d.log||[]).reverse();
    document.getElementById('log').innerHTML = lines.map(l => {
      let cls = 'log-line';
      if (l.includes('WIN')) cls += ' win';
      if (l.includes('LOSS')) cls += ' loss';
      if (l.includes('BET')) cls += ' bet';
      return '<div class="'+cls+'">'+escapeHtml(l)+'</div>';
    }).join('');
  } catch(e) {
    document.getElementById('status').textContent = 'Error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Arbitrage Dashboard ──────────────────────────────────────────────

@app.get("/_internal/arbitrage/status")
async def get_arbitrage_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    signals_file = Path(__file__).parent / "signals.json"
    result = {"running": False, "total_signals": 0, "signals": [], "last_scan": ""}
    if signals_file.exists():
        try:
            with open(signals_file) as f:
                signals = json.load(f)
            result["running"] = True
            result["total_signals"] = len(signals)
            result["signals"] = signals[-100:]  # last 100
            if signals:
                result["last_scan"] = signals[-1].get("timestamp", "")
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
            print(f"  [SIGNALS] Error reading signals: {e}")
    return result


@app.get("/arbitrage")
async def arbitrage_dashboard(request: Request):
    """Redirect to standalone Sports Dashboard on port 8888."""
    return RedirectResponse("/", status_code=302)


# ─── Weather Dashboard ────────────────────────────────────────────────

@app.get("/_internal/weather/status")
async def get_weather_status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    db_path = Path(__file__).parent.parent / "polymarket_weather_bot" / "trades.db"
    result = {"running": False, "signals": [], "trades": [], "total_signals": 0, "total_trades": 0}
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            result["running"] = True
            # Recent signals
            rows = conn.execute("SELECT * FROM signals ORDER BY created_at DESC LIMIT 50").fetchall()
            result["signals"] = [dict(r) for r in rows]
            result["total_signals"] = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            # Recent trades
            try:
                rows = conn.execute("SELECT * FROM trades ORDER BY created_at DESC LIMIT 50").fetchall()
                result["trades"] = [dict(r) for r in rows]
                result["total_trades"] = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            except Exception:
                pass
            conn.close()
        except Exception as e:
            result["error"] = str(e)
    return result


@app.get("/weather")
async def weather_dashboard(request: Request):
    """Redirect to standalone Weather Dashboard on port 5050."""
    return RedirectResponse("http://localhost:5050", status_code=302)


# ─── Dashboard Hub ───────────────────────────────────────────────────

@app.get("/hub", response_class=HTMLResponse)
async def dashboard_hub(request: Request):
    """Central hub linking to all 4 dashboards on their dedicated ports."""
    host = html_mod.escape(request.headers.get("host", "localhost:8000").split(":")[0])
    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard Hub</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#0a0e17; color:#e1e5ee; font-family:'SF Mono','Fira Code',monospace; display:flex; justify-content:center; align-items:center; min-height:100vh; }}
  .hub {{ max-width:600px; width:100%; padding:40px; }}
  h1 {{ font-size:1.6rem; margin-bottom:8px; background:linear-gradient(90deg,#60a5fa,#a78bfa); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
  .subtitle {{ color:#64748b; font-size:0.85rem; margin-bottom:32px; }}
  .cards {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .card {{ background:#111827; border:1px solid #1e2940; border-radius:12px; padding:24px; text-decoration:none; color:#e1e5ee; transition:border-color 0.2s, transform 0.2s; }}
  .card:hover {{ border-color:#60a5fa; transform:translateY(-2px); }}
  .card h2 {{ font-size:1rem; margin-bottom:6px; }}
  .card .port {{ color:#64748b; font-size:0.75rem; margin-bottom:8px; }}
  .card .desc {{ color:#94a3b8; font-size:0.8rem; line-height:1.4; }}
  .card.crypto h2 {{ color:#f7931a; }}
  .card.stock h2 {{ color:#60a5fa; }}
  .card.weather h2 {{ color:#4da6ff; }}
  .card.sports h2 {{ color:#ffaa00; }}
</style>
</head><body>
<div class="hub">
  <h1>Polymarket Dashboards</h1>
  <p class="subtitle">4 dashboards, each on its own port</p>
  <div class="cards">
    <a href="http://{host}:8000" class="card crypto">
      <h2>Crypto Dashboard</h2>
      <div class="port">Port 8000</div>
      <div class="desc">BTC/ETH analysis, trading bot, and crypto signals</div>
    </a>
    <a href="http://{host}:8050" class="card stock">
      <h2>Stock Prediction</h2>
      <div class="port">Port 8050</div>
      <div class="desc">Stock prediction bot with P/L tracking</div>
    </a>
    <a href="http://{host}:5050" class="card weather">
      <h2>Weather Trading</h2>
      <div class="port">Port 5050</div>
      <div class="desc">Weather forecast vs Polymarket odds</div>
    </a>
    <a href="http://{host}:8888" class="card sports">
      <h2>Sports Betting</h2>
      <div class="port">Port 8888</div>
      <div class="desc">Bookmaker vs Polymarket odds comparison</div>
    </a>
  </div>
</div>
</body></html>"""
    return HTMLResponse(html)


# ─── Kalshi Markets Dashboard ────────────────────────────────────────

@app.get("/kalshi", response_class=HTMLResponse)
async def kalshi_dashboard(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    user = _get_session_user(request)

    try:
        from kalshi_scanner import run_scanner as kalshi_scan
        data = await asyncio.to_thread(kalshi_scan)
    except Exception as e:
        data = {"total_markets": 0, "trending": [], "close_calls": [], "top_events": [], "categories": {}}

    # Build market rows
    trending_rows = ""
    for m in (data.get("trending") or [])[:25]:
        yes_cls = "positive" if m["yes_price"] >= 0.5 else "negative"
        trending_rows += f"""<tr>
          <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;">{html_mod.escape(m['title'][:70])}</td>
          <td class="{yes_cls}" style="font-weight:700;">{m['yes_price']:.0%}</td>
          <td>{1-m['yes_price']:.0%}</td>
          <td>{m.get('volume_24h',0):,}</td>
          <td>{m.get('volume',0):,}</td>
          <td style="color:var(--muted);font-size:0.75em;">{html_mod.escape(m.get('category',''))}</td>
        </tr>"""

    close_rows = ""
    for m in (data.get("close_calls") or [])[:20]:
        close_rows += f"""<tr>
          <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;">{html_mod.escape(m['title'][:70])}</td>
          <td style="font-weight:700;">{m['yes_price']:.0%}</td>
          <td>{1-m['yes_price']:.0%}</td>
          <td>{m.get('volume',0):,}</td>
          <td style="color:var(--muted);font-size:0.75em;">{html_mod.escape(m.get('category',''))}</td>
        </tr>"""

    cat_cards = ""
    for cat, info in list((data.get("categories") or {}).items())[:12]:
        cat_cards += f'<div class="card"><div class="label">{html_mod.escape(cat)}</div><div class="value">{info["count"]}</div><div class="detail">Vol: {info["total_volume"]:,}</div></div>'

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Kalshi Markets</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:8px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.3em;font-weight:700;margin-top:2px; }}
  .card .detail {{ color:var(--muted);font-size:0.7em;margin-top:2px; }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  .section {{ margin-bottom:24px; }}
  .section h2 {{ font-size:1em;color:var(--blue);margin-bottom:10px; }}
</style></head><body>
<div class="nav">
  <div class="nav-links">
    <a href="/polybot">Polymarket Bot</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Kalshi Prediction Markets</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:16px;">{data.get('total_markets',0):,} active markets &bull; Updated {datetime.now(timezone.utc).strftime('%H:%M UTC')}</p>

<div class="section">
  <h2>Categories</h2>
  <div class="cards">{cat_cards}</div>
</div>

<div class="section">
  <h2>Trending (24h Volume)</h2>
  <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;">
    <table>
      <thead><tr><th>Market</th><th>Yes</th><th>No</th><th>24h Vol</th><th>Total Vol</th><th>Category</th></tr></thead>
      <tbody>{trending_rows}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2>Close Calls (35-65% odds)</h2>
  <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;">
    <table>
      <thead><tr><th>Market</th><th>Yes</th><th>No</th><th>Volume</th><th>Category</th></tr></thead>
      <tbody>{close_rows}</tbody>
    </table>
  </div>
</div>

<script>setInterval(()=>location.reload(),300000);</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Trade Page (Polymarket) ─────────────────────────────────────────

@app.get("/trade", response_class=HTMLResponse)
async def trade_page(request: Request):
    """Polymarket trading page — browse markets and follow/place trades."""
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    user = _get_session_user(request)
    is_premium = user and user["tier"] in ("premium", "admin")

    # Get suspicious trades for "follow trade" feature (free for all users)
    sus_trades = []
    if last_sus_scan and last_sus_scan.get("suspicious_trades"):
        sus_trades = last_sus_scan["suspicious_trades"][:20]

    # Get active Polymarket markets
    try:
        from suspicious_trades import get_active_markets
        markets = await asyncio.to_thread(get_active_markets, 100)
    except Exception:
        markets = []

    # Build suspicious trades rows (premium only)
    sus_rows = ""
    for t in sus_trades:
        odds_str = t.get("odds_str", f'{t.get("price", 0):.0%}')
        pot_profit = t.get("potential_profit", 0)
        slug = html_mod.escape(t.get("market_id", ""), quote=True)
        poly_url = f"https://polymarket.com/event/{slug}" if slug else "#"
        sus_rows += f"""<tr>
          <td style="max-width:250px;overflow:hidden;text-overflow:ellipsis;">{html_mod.escape(t['title'][:60])}</td>
          <td>{html_mod.escape(str(t['outcome']))}</td>
          <td style="font-weight:600;">{odds_str}</td>
          <td style="font-weight:700;">${t['usd_value']:,.0f}</td>
          <td class="negative" style="font-weight:700;">${pot_profit:,.0f}</td>
          <td style="font-weight:700;color:var(--red);">{t['score']}</td>
          <td><a href="{poly_url}" target="_blank" style="background:var(--blue);color:#fff;padding:4px 10px;border-radius:4px;text-decoration:none;font-size:0.8em;white-space:nowrap;">Trade on Polymarket</a></td>
        </tr>"""

    # Build active markets rows
    market_rows = ""
    for m in markets[:50]:
        slug = html_mod.escape(m.get("slug", ""), quote=True)
        poly_url = f"https://polymarket.com/event/{slug}" if slug else "#"
        vol = m.get("volume_24h", 0)
        vol_str = f"${vol:,.0f}" if vol >= 1000 else f"${vol:.0f}"
        market_rows += f"""<tr>
          <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;">{html_mod.escape(m['question'][:70])}</td>
          <td>{vol_str}</td>
          <td>${m.get('liquidity', 0):,.0f}</td>
          <td><a href="{poly_url}" target="_blank" style="color:var(--blue);text-decoration:none;font-size:0.85em;">Open →</a></td>
        </tr>"""

    premium_badge = '<span style="background:var(--green);color:#000;padding:1px 8px;border-radius:4px;font-size:0.7em;font-weight:600;margin-left:8px;">PREMIUM</span>' if is_premium else ""

    sus_section = ""
    if sus_rows:
        sus_section = f"""
        <div class="section">
          <h2 style="color:var(--red);">Follow Suspicious Trades</h2>
          <p style="color:var(--muted);font-size:0.8em;margin-bottom:10px;">
            Trades flagged by our scanner as potentially suspicious. Click "Trade on Polymarket" to follow these trades on the same markets.
          </p>
          <div style="overflow-x:auto;border:1px solid var(--red);border-radius:8px;">
            <table>
              <thead><tr><th>Market</th><th>Outcome</th><th>Odds</th><th>Bet Size</th><th>Potential Profit</th><th>Score</th><th>Action</th></tr></thead>
              <tbody>{sus_rows}</tbody>
            </table>
          </div>
        </div>"""

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Trade</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:8px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  .section {{ margin-bottom:24px; }}
  .section h2 {{ font-size:1em;color:var(--blue);margin-bottom:10px; }}
</style></head><body>
<div class="nav">
  <div class="nav-links">
    <a href="/">Dashboard</a>
    <a href="/kalshi">Kalshi</a>
    <a href="/trade" class="active">Trade</a>
    <a href="/accuracy">Accuracy</a>
    <a href="/settings">Settings</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Polymarket Trading</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:16px;">{len(markets)} active markets &bull; Browse and trade directly on Polymarket</p>

<div style="background:rgba(210,153,34,0.1);border:1px solid rgba(210,153,34,0.3);border-radius:8px;padding:10px 16px;margin-bottom:20px;font-size:0.75em;color:var(--yellow);">
  &#9888; <strong>Not financial advice.</strong> Trading prediction markets involves risk. Never bet more than you can afford to lose.
</div>

{sus_section}

<div class="section">
  <h2 style="color:var(--red);">News-Trade Insider Alerts <span style="display:inline-block;width:8px;height:8px;background:var(--red);border-radius:50%;margin-left:6px;animation:pulse 1s infinite;"></span></h2>
  <p style="color:var(--muted);font-size:0.75em;margin-bottom:4px;">
    Scans news for insider trading reports, suspicious bets &amp; prediction market anomalies. Cross-referenced with live Polymarket data. Scans every 20 min.
  </p>
  <div style="display:flex;gap:8px;margin-bottom:10px;align-items:center;">
    <button onclick="toggleWatchlistView()" id="wl-toggle-btn" style="background:var(--card);color:var(--blue);border:1px solid var(--blue);padding:4px 12px;border-radius:4px;cursor:pointer;font-size:0.75em;">Show Watchlist</button>
    <span id="wl-count" style="color:var(--muted);font-size:0.7em;"></span>
  </div>
  <div id="news-trade-alerts" style="border:1px solid var(--red);border-radius:8px;max-height:500px;overflow-y:auto;">
    <div style="padding:16px;color:var(--muted);text-align:center;">Loading alerts...</div>
  </div>
  <div id="news-trade-watchlist" style="display:none;border:1px solid var(--blue);border-radius:8px;max-height:400px;overflow-y:auto;margin-top:10px;">
    <div style="padding:16px;color:var(--muted);text-align:center;">Loading watchlist...</div>
  </div>
</div>

<div class="section">
  <h2>Active Polymarket Markets (by 24h Volume)</h2>
  <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;">
    <table>
      <thead><tr><th>Market</th><th>24h Volume</th><th>Liquidity</th><th>Action</th></tr></thead>
      <tbody>{market_rows}</tbody>
    </table>
  </div>
</div>

<div class="section">
  <h2 style="color:var(--yellow);">Current Affairs Feed <span id="news-pulse" style="display:inline-block;width:8px;height:8px;background:var(--green);border-radius:50%;margin-left:6px;animation:pulse 1s infinite;"></span></h2>
  <p style="color:var(--muted);font-size:0.75em;margin-bottom:8px;">Auto-refreshes every 5 seconds &bull; Sources: BBC, NYT, Reuters</p>
  <div id="news-feed" style="border:1px solid var(--border);border-radius:8px;max-height:400px;overflow-y:auto;">
    <div style="padding:16px;color:var(--muted);text-align:center;">Loading news...</div>
  </div>
</div>

<style>
  @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:0.3}} }}
  .news-item {{ padding:10px 14px;border-bottom:1px solid var(--border);transition:background 0.2s; }}
  .news-item:last-child {{ border-bottom:none; }}
  .news-item:hover {{ background:rgba(88,166,255,0.05); }}
  .news-item .news-title {{ font-size:0.85em;font-weight:600;margin-bottom:3px; }}
  .news-item .news-title a {{ color:var(--text);text-decoration:none; }}
  .news-item .news-title a:hover {{ color:var(--blue); }}
  .news-item .news-meta {{ font-size:0.7em;color:var(--muted); }}
  .news-item .news-summary {{ font-size:0.75em;color:var(--muted);margin-top:3px; }}
  .news-new {{ animation:fadeIn 0.4s ease-in; }}
  @keyframes fadeIn {{ from{{opacity:0;transform:translateY(-5px)}} to{{opacity:1;transform:translateY(0)}} }}
</style>

<script>
(function() {{
  const feed = document.getElementById('news-feed');
  let knownTitles = new Set();
  let firstLoad = true;

  function escapeHtml(s) {{
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }}

  async function refreshNews() {{
    try {{
      const resp = await fetch('/api/news');
      if (!resp.ok) return;
      const data = await resp.json();
      const articles = data.articles || [];
      if (!articles.length) return;

      const newTitles = new Set(articles.map(a => a.title));
      let html = '';
      for (const a of articles) {{
        const isNew = !firstLoad && !knownTitles.has(a.title);
        const src = escapeHtml(a.source);
        const pub = a.published ? new Date(a.published).toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}}) : '';
        html += '<div class="news-item' + (isNew ? ' news-new' : '') + '">'
          + '<div class="news-title"><a href="' + escapeHtml(a.link) + '" target="_blank">' + escapeHtml(a.title) + '</a></div>'
          + '<div class="news-meta">' + src + (pub ? ' &bull; ' + pub : '') + '</div>'
          + (a.summary ? '<div class="news-summary">' + escapeHtml(a.summary.replace(/<[^>]*>/g,'')) + '</div>' : '')
          + '</div>';
      }}
      feed.innerHTML = html;
      knownTitles = newTitles;
      firstLoad = false;
    }} catch(e) {{ /* retry next cycle */ }}
  }}

  refreshNews();
  setInterval(refreshNews, 5000);
}})();
</script>

<style>
  /* News-Trade Alert styles */
  .nta-item {{ padding:12px 14px;border-bottom:1px solid var(--border);transition:background 0.2s; }}
  .nta-item:last-child {{ border-bottom:none; }}
  .nta-item:hover {{ background:rgba(248,81,73,0.05); }}
  .nta-header {{ display:flex;justify-content:space-between;align-items:flex-start;gap:8px; }}
  .nta-title {{ font-size:0.85em;font-weight:600;flex:1; }}
  .nta-title a {{ color:var(--text);text-decoration:none; }}
  .nta-title a:hover {{ color:var(--red); }}
  .nta-score {{ background:var(--red);color:#fff;padding:2px 8px;border-radius:4px;font-size:0.7em;font-weight:700;white-space:nowrap; }}
  .nta-score.medium {{ background:var(--yellow);color:#000; }}
  .nta-score.low {{ background:var(--border);color:var(--muted); }}
  .nta-meta {{ font-size:0.7em;color:var(--muted);margin-top:3px; }}
  .nta-reasons {{ font-size:0.72em;color:var(--muted);margin-top:4px;padding-left:12px; }}
  .nta-reasons span {{ display:inline-block;background:rgba(248,81,73,0.1);border:1px solid rgba(248,81,73,0.2);border-radius:3px;padding:1px 6px;margin:2px 4px 2px 0;font-size:0.9em; }}
  .nta-markets {{ margin-top:6px;padding:6px 10px;background:rgba(88,166,255,0.05);border-radius:4px;font-size:0.72em; }}
  .nta-markets a {{ color:var(--blue);text-decoration:none; }}
  .nta-markets a:hover {{ text-decoration:underline; }}
  .nta-actions {{ margin-top:6px;display:flex;gap:6px; }}
  .nta-watch-btn {{ background:none;border:1px solid var(--blue);color:var(--blue);padding:2px 10px;border-radius:4px;cursor:pointer;font-size:0.72em;transition:all 0.2s; }}
  .nta-watch-btn:hover {{ background:var(--blue);color:#fff; }}
  .nta-watch-btn.watched {{ background:var(--blue);color:#fff; }}
  .nta-unwatch-btn {{ background:none;border:1px solid var(--red);color:var(--red);padding:2px 10px;border-radius:4px;cursor:pointer;font-size:0.72em; }}
  .nta-unwatch-btn:hover {{ background:var(--red);color:#fff; }}
  .nta-trade {{ margin-top:6px;padding:8px 10px;background:rgba(248,81,73,0.06);border:1px solid rgba(248,81,73,0.15);border-radius:6px; }}
  .nta-trade-label {{ font-size:0.6em;text-transform:uppercase;letter-spacing:1px;color:var(--red);font-weight:700;margin-bottom:3px; }}
  .nta-trade-detail {{ font-size:0.8em;margin-bottom:4px; }}
  .nta-trade-stats {{ display:flex;flex-wrap:wrap;gap:10px;font-size:0.72em;color:var(--muted); }}
  .nta-trade-stats b {{ color:var(--text); }}
  .nta-trade-stats code {{ background:var(--card);padding:1px 4px;border-radius:3px;font-size:0.9em; }}

  /* Toast notification */
  #toast-container {{ position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none; }}
  .toast {{ pointer-events:auto;background:var(--card);border:1px solid var(--red);border-radius:8px;padding:12px 16px;max-width:380px;box-shadow:0 4px 24px rgba(0,0,0,0.5);animation:slideIn 0.4s ease-out; }}
  .toast.news-trade {{ border-color:var(--yellow); }}
  .toast-title {{ font-size:0.85em;font-weight:700;margin-bottom:4px; }}
  .toast-body {{ font-size:0.75em;color:var(--muted); }}
  .toast-close {{ position:absolute;top:8px;right:12px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:1em; }}
  @keyframes slideIn {{ from{{opacity:0;transform:translateX(50px)}} to{{opacity:1;transform:translateX(0)}} }}
</style>

<div id="toast-container"></div>

<script>
(function() {{
  const alertsEl = document.getElementById('news-trade-alerts');
  const watchlistEl = document.getElementById('news-trade-watchlist');
  const wlCountEl = document.getElementById('wl-count');
  let watchedIds = new Set();
  let showingWatchlist = false;

  function escapeHtml(s) {{
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }}

  function scoreClass(score) {{
    if (score >= 50) return '';
    if (score >= 25) return ' medium';
    return ' low';
  }}

  function renderAlert(a, isWatchlist) {{
    const scoreHtml = '<span class="nta-score' + scoreClass(a.score) + '">' + a.score + '/100</span>';

    // Trade details (if present — v2 scanner always includes these)
    let tradeHtml = '';
    if (a.trade_title) {{
      const profitStr = a.trade_potential_profit ? '$' + Number(a.trade_potential_profit).toLocaleString() : '?';
      const sizeStr = a.trade_size ? '$' + Number(a.trade_size).toLocaleString() : '?';
      const oddsStr = a.trade_odds_str || (a.trade_odds ? (a.trade_odds * 100).toFixed(0) + '%' : '?');
      const polyUrl = a.trade_market_id ? 'https://polymarket.com/event/' + encodeURIComponent(a.trade_market_id) : '#';
      tradeHtml = '<div class="nta-trade">'
        + '<div class="nta-trade-label">SUSPICIOUS TRADE</div>'
        + '<div class="nta-trade-detail">'
        + '<a href="' + polyUrl + '" target="_blank" style="color:var(--blue);text-decoration:none;font-weight:600;">' + escapeHtml(a.trade_title.substring(0, 60)) + '</a>'
        + ' &mdash; ' + escapeHtml(a.trade_outcome || '')
        + '</div>'
        + '<div class="nta-trade-stats">'
        + '<span>Bet: <b>' + sizeStr + '</b></span>'
        + '<span>Odds: <b>' + oddsStr + '</b></span>'
        + '<span>Profit: <b style="color:var(--red);">' + profitStr + '</b></span>'
        + (a.trade_time ? '<span>Placed: ' + escapeHtml(a.trade_time) + '</span>' : '')
        + (a.trade_wallet ? '<span>Wallet: <code>' + escapeHtml(a.trade_wallet) + '...</code></span>' : '')
        + '</div>'
        + '</div>';
    }}

    const reasons = (a.reasons || []).map(r => '<span>' + escapeHtml(r) + '</span>').join('');

    const watched = watchedIds.has(a.id || a.alert_id);
    let actionBtn = '';
    if (isWatchlist) {{
      actionBtn = '<button class="nta-unwatch-btn" onclick="removeFromWatchlist(\'' + (a.alert_id || a.id) + '\')">Remove</button>';
    }} else {{
      actionBtn = watched
        ? '<button class="nta-watch-btn watched" disabled>Watching</button>'
        : '<button class="nta-watch-btn" onclick="addToWatchlist(\'' + a.id + '\')">+ Watch</button>';
    }}

    return '<div class="nta-item">'
      + '<div class="nta-header">'
      + '<div class="nta-title"><a href="' + escapeHtml(a.link) + '" target="_blank">' + escapeHtml(a.title) + '</a></div>'
      + scoreHtml
      + '</div>'
      + '<div class="nta-meta">' + escapeHtml(a.source) + (a.published ? ' &bull; ' + escapeHtml(a.published) : '') + '</div>'
      + tradeHtml
      + (reasons ? '<div class="nta-reasons">' + reasons + '</div>' : '')
      + '<div class="nta-actions">' + actionBtn + '</div>'
      + '</div>';
  }}

  async function loadAlerts() {{
    try {{
      const resp = await fetch('/api/news-trade-alerts?min_score=10');
      if (!resp.ok) return;
      const data = await resp.json();
      const alerts = data.alerts || [];
      if (!alerts.length) {{
        alertsEl.innerHTML = '<div style="padding:16px;color:var(--muted);text-align:center;">No insider trading alerts detected yet. Scanner runs every 20 minutes.</div>';
        return;
      }}
      alertsEl.innerHTML = alerts.map(a => renderAlert(a, false)).join('');
    }} catch(e) {{}}
  }}

  async function loadWatchlist() {{
    try {{
      const resp = await fetch('/api/news-watchlist');
      if (!resp.ok) return;
      const data = await resp.json();
      const items = data.watchlist || [];
      watchedIds = new Set(items.map(w => w.alert_id));
      wlCountEl.textContent = items.length ? items.length + ' watched' : '';
      if (!items.length) {{
        watchlistEl.innerHTML = '<div style="padding:16px;color:var(--muted);text-align:center;">Your watchlist is empty. Click "+ Watch" on any alert above to track it.</div>';
      }} else {{
        watchlistEl.innerHTML = items.map(w => renderAlert(w, true)).join('');
      }}
    }} catch(e) {{}}
  }}

  window.addToWatchlist = async function(alertId) {{
    try {{
      const resp = await fetch('/api/news-watchlist/add', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{alert_id: alertId}})
      }});
      if (resp.ok) {{
        watchedIds.add(alertId);
        loadAlerts();
        loadWatchlist();
      }}
    }} catch(e) {{}}
  }};

  window.removeFromWatchlist = async function(alertId) {{
    try {{
      const resp = await fetch('/api/news-watchlist/remove', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{alert_id: alertId}})
      }});
      if (resp.ok) {{
        watchedIds.delete(alertId);
        loadAlerts();
        loadWatchlist();
      }}
    }} catch(e) {{}}
  }};

  window.toggleWatchlistView = function() {{
    showingWatchlist = !showingWatchlist;
    watchlistEl.style.display = showingWatchlist ? 'block' : 'none';
    document.getElementById('wl-toggle-btn').textContent = showingWatchlist ? 'Hide Watchlist' : 'Show Watchlist';
  }};

  // Toast notification system
  function showToast(title, body, type) {{
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast' + (type === 'news_trade' ? ' news-trade' : '');
    toast.style.position = 'relative';
    toast.innerHTML = '<button class="toast-close" onclick="this.parentElement.remove()">&times;</button>'
      + '<div class="toast-title">' + escapeHtml(title) + '</div>'
      + '<div class="toast-body">' + escapeHtml(body) + '</div>';
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 15000);
  }}

  // WebSocket listener for real-time push notifications
  function connectWS() {{
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(proto + '//' + location.host + '/ws');
    ws.onmessage = function(e) {{
      try {{
        const msg = JSON.parse(e.data);
        if (msg.type === 'alert' && msg.data) {{
          const d = msg.data;
          if (d.direction === 'news_trade') {{
            showToast(
              'Insider Alert [' + d.confidence + '/100]',
              d.delta || 'New suspicious trading news detected',
              'news_trade'
            );
            // Refresh alerts list
            loadAlerts();
            loadWatchlist();
          }} else if (d.direction === 'suspicious') {{
            showToast(
              'Suspicious Trade [Score: ' + d.confidence + ']',
              d.delta || 'Large suspicious trade detected on Polymarket',
              'suspicious'
            );
          }}
        }}
      }} catch(e) {{}}
    }};
    ws.onclose = function() {{ setTimeout(connectWS, 5000); }};
    ws.onerror = function() {{ ws.close(); }};
  }}

  // Request browser notification permission
  if ('Notification' in window && Notification.permission === 'default') {{
    Notification.requestPermission();
  }}

  loadAlerts();
  loadWatchlist();
  setInterval(loadAlerts, 60000);  // refresh alerts every 60s
  setInterval(loadWatchlist, 60000);
  connectWS();
}})();
</script>

<script>setInterval(()=>location.reload(),300000);</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Current Affairs News Feed ───────────────────────────────────────

_news_cache: list = []
_news_cache_time: float = 0
_NEWS_CACHE_TTL = 60  # refresh from RSS every 60s

RSS_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "NYT"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
]


def _fetch_news_from_rss() -> list:
    """Fetch latest headlines from multiple RSS feeds."""
    articles = []
    for url, source in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": "CryptoEdge/1.0"})
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            # Standard RSS 2.0 structure
            for item in root.iter("item"):
                title_el = item.find("title")
                link_el = item.find("link")
                pub_el = item.find("pubDate")
                desc_el = item.find("description")
                if title_el is None or title_el.text is None:
                    continue
                articles.append({
                    "title": title_el.text.strip(),
                    "link": (link_el.text or "").strip() if link_el is not None else "",
                    "source": source,
                    "published": (pub_el.text or "").strip() if pub_el is not None else "",
                    "summary": (desc_el.text or "").strip()[:200] if desc_el is not None else "",
                })
        except Exception:
            continue
    # Sort by published date (most recent first), limit to 30
    articles.sort(key=lambda a: a.get("published", ""), reverse=True)
    return articles[:30]


@app.get("/api/news")
async def api_news(request: Request):
    """Return cached current affairs headlines as JSON."""
    global _news_cache, _news_cache_time
    if not _check_auth(request):
        raise HTTPException(status_code=401)
    now = time.time()
    if now - _news_cache_time > _NEWS_CACHE_TTL or not _news_cache:
        _news_cache = await asyncio.to_thread(_fetch_news_from_rss)
        _news_cache_time = now
    return JSONResponse({"articles": _news_cache, "updated": _news_cache_time})


# ─── News-Trade Alerts API ──────────────────────────────────────────

@app.get("/api/news-trade-alerts")
async def api_news_trade_alerts(request: Request):
    """Return news-trade correlation alerts."""
    if not _check_auth(request):
        raise HTTPException(status_code=401)
    try:
        min_score = int(request.query_params.get("min_score", "0"))
    except ValueError:
        return JSONResponse({"error": "Invalid min_score"}, status_code=400)
    try:
        hours = int(request.query_params.get("hours", "72"))
    except ValueError:
        return JSONResponse({"error": "Invalid hours"}, status_code=400)
    min_score = max(0, min(min_score, 100))
    hours = min(hours, 720)
    alerts = await asyncio.to_thread(db.get_news_alerts, min_score, 50, hours)
    return JSONResponse({"alerts": alerts, "updated": last_news_trade_time})


@app.get("/api/news-watchlist")
async def api_get_watchlist(request: Request):
    """Get the current user's news-trade watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    items = await asyncio.to_thread(db.get_news_watchlist, user["id"])
    return JSONResponse({"watchlist": items})


@app.post("/api/news-watchlist/add")
async def api_add_to_watchlist(request: Request):
    """Add an alert to the user's watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    alert_id = body.get("alert_id", "")
    if not alert_id:
        raise HTTPException(status_code=400, detail="alert_id required")
    notes = body.get("notes", "")
    ok = await asyncio.to_thread(
        db.add_to_news_watchlist, user["id"], alert_id, notes
    )
    return JSONResponse({"ok": ok})


@app.post("/api/news-watchlist/remove")
async def api_remove_from_watchlist(request: Request):
    """Remove an alert from the user's watchlist."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    alert_id = body.get("alert_id", "")
    if not alert_id:
        raise HTTPException(status_code=400, detail="alert_id required")
    await asyncio.to_thread(db.remove_from_news_watchlist, user["id"], alert_id)
    return JSONResponse({"ok": True})


# ─── Accuracy Tracker ────────────────────────────────────────────────

@app.get("/accuracy", response_class=HTMLResponse)
async def accuracy_page(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    user = _get_session_user(request)

    # Get accuracy stats for each ticker
    stats_html = ""
    overall = db.get_accuracy_stats(days=30)
    for ticker in ASSETS:
        s = db.get_accuracy_stats(ticker=ticker, days=30)
        if s["total"] == 0:
            acc_cls = "muted"
            acc_str = "No data"
            hc_str = "—"
        else:
            acc_cls = "positive" if s["accuracy"] >= 0.53 else ("negative" if s["accuracy"] < 0.50 else "yellow")
            acc_str = f'{s["accuracy"]*100:.1f}%'
            hc_str = f'{s["high_conf_accuracy"]*100:.1f}% ({s["high_conf_total"]})' if s["high_conf_total"] else "—"

        stats_html += f"""<div class="card">
          <div class="label">{ticker}</div>
          <div class="value {acc_cls}">{acc_str}</div>
          <div class="detail">{s['total']} predictions | HC: {hc_str}</div>
        </div>"""

    # Recent predictions
    recent = db.get_recent_predictions(limit=50)
    recent_rows = ""
    for p in recent:
        if p["was_correct"] is not None:
            correct_cls = "positive" if p["was_correct"] else "negative"
            correct_str = "&#10003;" if p["was_correct"] else "&#10007;"
        else:
            correct_cls = "muted"
            correct_str = "pending"
        dir_cls = "positive" if p["pred_direction"] == "positive" else "negative"
        conf_pct = (p["confidence"] or 0) * 100
        recent_rows += f"""<tr>
          <td>{p['ticker']}</td>
          <td>{p['window_start'][:16]}</td>
          <td class="{dir_cls}">{p['pred_direction'].upper()}</td>
          <td>${p['pred_delta']:+,.2f}</td>
          <td>{conf_pct:.0f}%</td>
          <td>{p.get('actual_direction','—') or '—'}</td>
          <td class="{correct_cls}" style="font-weight:700;">{correct_str}</td>
        </tr>"""

    ov_acc = f'{overall["accuracy"]*100:.1f}%' if overall["total"] else "No data"
    ov_cls = "positive" if overall.get("accuracy",0) >= 0.53 else ("negative" if overall.get("accuracy",0) < 0.50 else "yellow")

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Accuracy Tracker</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--yellow:#d29922; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap;justify-content:space-between; }}
  .nav-links {{ display:flex;gap:16px; }}
  .nav a {{ color:var(--muted);text-decoration:none; }} .nav a.active {{ color:var(--blue);font-weight:600; }}
  h1 {{ font-size:1.4em;margin-bottom:4px; }}
  .positive {{ color:var(--green); }} .negative {{ color:var(--red); }} .yellow {{ color:var(--yellow); }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.4em;font-weight:700;margin-top:2px; }}
  .card .detail {{ color:var(--muted);font-size:0.7em;margin-top:2px; }}
  table {{ width:100%;border-collapse:collapse;font-size:0.82em; }}
  th {{ background:var(--card);color:var(--muted);text-transform:uppercase;font-size:0.7em;padding:10px 8px;text-align:left; }}
  td {{ padding:6px 8px;border-top:1px solid var(--border); }}
  tr:hover td {{ background:rgba(88,166,255,0.05); }}
  .hero {{ background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;text-align:center;margin-bottom:20px; }}
</style></head><body>
<div class="nav">
  <div class="nav-links">
    <a href="/polybot">Polymarket Bot</a>
  </div>
  <div class="nav-links">
    <a href="/logout" style="color:var(--red);">Logout</a>
  </div>
</div>

<h1>Model Accuracy Tracker</h1>
<p style="color:var(--muted);font-size:0.85em;margin-bottom:16px;">Live tracking of every prediction vs actual outcome — 30 day window</p>

<div class="hero">
  <div style="color:var(--muted);font-size:0.8em;text-transform:uppercase;">Overall Accuracy (30d)</div>
  <div style="font-size:2.5em;font-weight:800;" class="{ov_cls}">{ov_acc}</div>
  <div style="color:var(--muted);font-size:0.85em;margin-top:4px;">{overall['total']:,} total predictions | {overall['correct']:,} correct</div>
</div>

<h2 style="font-size:1em;color:var(--blue);margin-bottom:8px;">Per-Asset Accuracy</h2>
<div class="cards">{stats_html}</div>

<h2 style="font-size:1em;color:var(--blue);margin-bottom:8px;margin-top:24px;">Recent Predictions</h2>
<div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px;max-height:60vh;overflow-y:auto;">
  <table>
    <thead><tr><th>Asset</th><th>Window</th><th>Predicted</th><th>Delta</th><th>Conf</th><th>Actual</th><th>Result</th></tr></thead>
    <tbody>{recent_rows if recent_rows else '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:20px;">Predictions will appear here as the models run. Data is logged every 5 minutes.</td></tr>'}</tbody>
  </table>
</div>

<script>setInterval(()=>location.reload(),60000);</script>
</body></html>"""
    return HTMLResponse(html)


# ─── Settings / Watchlist ────────────────────────────────────────────

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not _check_auth(request):
        return RedirectResponse("https://habbig.com/login", status_code=302)
    user = _get_session_user(request)
    watchlists = db.get_watchlists(user["id"])
    alert_prefs = db.get_alert_prefs(user["id"])

    wl_html = ""
    for wl in watchlists:
        tickers = json.loads(wl["tickers"]) if isinstance(wl["tickers"], str) else wl["tickers"]
        wl_html += f'<div class="card"><div class="label">{wl["name"]}</div><div class="value" style="font-size:1em;">{", ".join(tickers)}</div></div>'
    if not wl_html:
        wl_html = '<div style="color:var(--muted);">No watchlists yet.</div>'

    tier_esc = html_mod.escape(user["tier"].upper())
    tier_badge = f'<span style="background:{"var(--green)" if user["tier"]=="premium" else "var(--blue)"};color:#fff;padding:3px 10px;border-radius:12px;font-size:0.75em;font-weight:600;">{tier_esc}</span>'

    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoEdge — Settings</title>
<style>
  :root {{ --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--blue:#58a6ff; }}
  * {{ margin:0;padding:0;box-sizing:border-box; }}
  body {{ background:var(--bg);color:var(--text);font-family:-apple-system,'Segoe UI',sans-serif;padding:16px;max-width:700px;margin:0 auto; }}
  .nav {{ display:flex;gap:16px;font-size:0.85em;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border); }}
  .nav a {{ color:var(--muted);text-decoration:none; }}
  h1 {{ font-size:1.4em;margin-bottom:16px; }}
  h2 {{ font-size:1em;color:var(--blue);margin:20px 0 8px; }}
  .cards {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px; }}
  .card {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px; }}
  .card .label {{ color:var(--muted);font-size:0.7em;text-transform:uppercase; }}
  .card .value {{ font-size:1.1em;font-weight:600;margin-top:4px; }}
  .info-box {{ background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px; }}
</style></head><body>
<div class="nav">
  <a href="/">&larr; Dashboard</a> <a href="/settings" style="color:var(--blue);font-weight:600;">Settings</a>
</div>

<h1>Account Settings {tier_badge}</h1>

<div class="info-box">
  <div style="color:var(--muted);font-size:0.8em;">EMAIL</div>
  <div style="font-size:1.1em;margin-top:2px;">{html_mod.escape(user['email'])}</div>
</div>
<div class="info-box">
  <div style="color:var(--muted);font-size:0.8em;">NAME</div>
  <div style="font-size:1.1em;margin-top:2px;">{html_mod.escape(user['display_name'] or '(not set)')}</div>
</div>

<h2>Your Tier: {tier_esc}</h2>
<div class="info-box">
  {"<p style='color:var(--green);font-weight:600;'>Premium features active: Neural Net predictions, Suspicious Trades detector, Model Marketplace</p>" if user['tier'] in ('premium','admin') else "<p style='color:var(--muted);'>Free tier — upgrade to Premium for neural net predictions, suspicious trade alerts, and model marketplace.</p><p style='margin-top:8px;'><em>Contact admin to upgrade.</em></p>"}
</div>

<h2>Watchlists</h2>
<div class="cards">{wl_html}</div>

</body></html>"""
    return HTMLResponse(html)


# ─── Legal Pages ─────────────────────────────────────────────────────

LEGAL_STYLE = """<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --blue:#58a6ff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,'Segoe UI',sans-serif; padding:32px; max-width:800px; margin:0 auto; }
  h1 { font-size:1.6em; margin-bottom:8px; }
  h2 { font-size:1.1em; margin-top:24px; margin-bottom:8px; color:var(--blue); }
  p, li { line-height:1.7; color:var(--muted); font-size:0.9em; margin-bottom:12px; }
  ul { padding-left:20px; }
  a { color:var(--blue); text-decoration:none; }
  .back { display:inline-block; margin-bottom:20px; font-size:0.85em; }
  .updated { color:var(--muted); font-size:0.75em; margin-bottom:24px; }
</style>"""

@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Terms of Service — CryptoEdge</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">&larr; Back to Dashboard</a>
<h1>Terms of Service</h1>
<p class="updated">Last updated: April 2026</p>

<h2>1. Acceptance of Terms</h2>
<p>By accessing CryptoEdge ("the Service"), you agree to be bound by these Terms of Service. If you do not agree, do not use the Service.</p>

<h2>2. Service Description</h2>
<p>CryptoEdge provides cryptocurrency market analysis using neural network ensembles trained on historical Binance data. The Service displays predictions, volatility metrics, and suspicious trade alerts for informational purposes only.</p>

<h2>3. No Financial Advice</h2>
<p><strong>The Service does not constitute financial advice, investment advice, trading advice, or any other sort of advice.</strong> You should not treat any of the Service's content as such. CryptoEdge does not recommend that any cryptocurrency should be bought, sold, or held by you. Nothing on this Service should be taken as an offer to buy, sell, or hold a cryptocurrency.</p>

<h2>4. No Guarantee of Accuracy</h2>
<p>Predictions, signals, and analysis are generated by machine learning models that are inherently probabilistic. Past accuracy does not guarantee future performance. The Service makes no warranty regarding the accuracy, completeness, or reliability of any information provided.</p>

<h2>5. Risk Acknowledgment</h2>
<p>Cryptocurrency trading involves substantial risk of loss and is not suitable for every investor. You acknowledge that:</p>
<ul>
<li>You may lose some or all of your invested capital</li>
<li>Past performance is not indicative of future results</li>
<li>The high degree of leverage in crypto trading can work against you as well as for you</li>
<li>You are solely responsible for any trading decisions you make</li>
</ul>

<h2>6. Account Security</h2>
<p>You are responsible for maintaining the confidentiality of your login credentials. You agree to notify us immediately of any unauthorized use of your account.</p>

<h2>7. Prohibited Use</h2>
<p>You may not: reverse-engineer the Service, redistribute data without permission, use the Service for market manipulation, or share access credentials.</p>

<h2>8. Limitation of Liability</h2>
<p>To the fullest extent permitted by law, CryptoEdge and its operators shall not be liable for any indirect, incidental, special, consequential, or punitive damages, including loss of profits, data, or funds, arising from your use of the Service.</p>

<h2>9. Changes to Terms</h2>
<p>We reserve the right to modify these terms at any time. Continued use of the Service after changes constitutes acceptance of the new terms.</p>
</body></html>""")


@app.get("/disclaimer", response_class=HTMLResponse)
async def disclaimer_page():
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>Risk Disclaimer — CryptoEdge</title>{LEGAL_STYLE}</head><body>
<a href="/" class="back">&larr; Back to Dashboard</a>
<h1>Risk Disclaimer</h1>
<p class="updated">Last updated: April 2026</p>

<div style="background:#1c1200;border:2px solid #d29922;border-radius:10px;padding:20px;margin-bottom:24px;">
<p style="color:#d29922;font-weight:700;font-size:1em;margin-bottom:8px;">&#9888; IMPORTANT WARNING</p>
<p style="color:#e6edf3;">Trading cryptocurrencies carries a high level of risk and may not be suitable for all investors. Before deciding to trade, you should carefully consider your investment objectives, level of experience, and risk appetite. <strong>The possibility exists that you could sustain a loss of some or all of your initial investment.</strong> You should not invest money that you cannot afford to lose.</p>
</div>

<h2>Model Limitations</h2>
<p>The neural network ensemble predictions displayed on this dashboard are based on historical price patterns. These models:</p>
<ul>
<li>Have been trained on historical data that may not reflect future market conditions</li>
<li>Cannot predict black swan events, regulatory changes, or market manipulation</li>
<li>Show directional accuracy of approximately 50-55%, which is marginally above random chance</li>
<li>Are retrained periodically and past accuracy metrics may not reflect current model performance</li>
</ul>

<h2>Data Sources</h2>
<p>Price data is sourced from Binance via their public API. Suspicious trade data is sourced from Polymarket's public CLOB API. We do not guarantee data availability, accuracy, or timeliness.</p>

<h2>Not Regulated Financial Product</h2>
<p>CryptoEdge is an analytics tool, not a regulated financial product. It is not registered with any financial regulatory authority. The operators are not licensed financial advisors.</p>

<h2>Your Responsibility</h2>
<p>You are solely responsible for your own trading decisions. Always do your own research (DYOR) and consider consulting a licensed financial advisor before making investment decisions.</p>
</body></html>""")


# ─── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Authenticate via gateway SSO headers (same as HTTP requests)
    _sso_secret = os.environ.get("GATEWAY_SSO_SECRET")
    headers = ws.headers
    authed = False
    if _sso_secret and hmac.compare_digest(headers.get("x-gateway-secret", ""), _sso_secret):
        if headers.get("x-gateway-user-id") and headers.get("x-gateway-user-email"):
            authed = True
    # Localhost bypass for bots — only when explicitly enabled via env var
    if not authed and os.getenv("DEV_LOCALHOST_BYPASS") == "1":
        client_host = ws.client.host if ws.client else ""
        if client_host in ("127.0.0.1", "::1", "localhost"):
            authed = True
    if not authed:
        await ws.close(code=4001, reason="Not authenticated")
        return
    await ws.accept()
    connected_ws.add(ws)
    try:
        # Send initial state
        await ws.send_text(json.dumps({
            "type": "init",
            "data": {ticker: serialize_asset(ticker) for ticker in asset_state},
        }))
        # Keep alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        connected_ws.discard(ws)
    except Exception:
        connected_ws.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

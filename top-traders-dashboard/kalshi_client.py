#!/usr/bin/env python3
"""
Self-contained Kalshi client for the top-traders dashboard.

Public side (no auth):  fetch_top_markets() — for the "Top Markets" tab.
Auth side (RSA-PSS):   KalshiClient(api_key, private_key_pem) — personal portfolio.

Auth protocol (per Kalshi docs):
  Sign string `<timestamp_ms><METHOD><path>` with RSA-PSS / SHA256, base64,
  send via headers KALSHI-ACCESS-{KEY,TIMESTAMP,SIGNATURE}.

Public market data is unauthenticated.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("top-traders.kalshi")

KALSHI_HOST = "https://api.elections.kalshi.com"
KALSHI_API_BASE = "/trade-api/v2"

_PUBLIC_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PolymarketTopTraders/1.0",
}


def _normalize_price(p) -> float:
    """Kalshi v2 prices arrive either as a 0-1 dollar string (yes_ask_dollars
    = '0.5234') or legacy cents 1-99. Normalize to 0-1 probability."""
    if p is None:
        return 0.0
    try:
        v = float(p)
    except (TypeError, ValueError):
        return 0.0
    if v > 1.0:
        v = v / 100.0
    return round(v, 4)


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ─── Public market data ─────────────────────────────────────────────────

def fetch_top_markets(limit: int = 100, status: str = "open") -> list[dict]:
    """Fetch open markets from Kalshi, sorted by 24h volume desc.

    Uses /events?with_nested_markets=true so we get real human-named events
    rather than the thousands of MVE multi-game parlay tickers that saturate
    the flat /markets endpoint. Returns a list of dicts with the columns the
    dashboard needs:
      ticker, title, subtitle, category, yes_price, no_price, yes_bid,
      yes_ask, volume, volume_24h, open_interest, close_time, event_ticker,
      event_title.
    """
    all_markets: list[dict] = []
    cursor: Optional[str] = None
    pages = 0
    # Each /events page returns up to 200 events; each event holds ≥1 nested
    # market. 8 pages × 200 events ≈ 1600 events / a few thousand markets.
    max_pages = 8
    page_size = 200

    with httpx.Client(timeout=20) as client:
        while pages < max_pages:
            params = {
                "limit": page_size,
                "status": status,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = client.get(
                    f"{KALSHI_HOST}{KALSHI_API_BASE}/events",
                    params=params,
                    headers=_PUBLIC_HEADERS,
                )
                if resp.status_code == 429:
                    time.sleep(2)
                    pages += 1
                    continue
                if resp.status_code != 200:
                    break
                data = resp.json()
            except httpx.HTTPError as e:
                log.warning("Kalshi /events fetch failed: %s", e)
                break

            events = data.get("events") or []
            for ev in events:
                ev_ticker = ev.get("event_ticker") or ""
                ev_title = ev.get("title") or ""
                ev_category = ev.get("category") or ""
                for m in (ev.get("markets") or []):
                    m["__event_ticker"] = ev_ticker
                    m["__event_title"] = ev_title
                    m["__event_category"] = ev_category
                    all_markets.append(m)
            cursor = data.get("cursor")
            pages += 1
            if not cursor or len(events) < page_size:
                break
            time.sleep(0.15)

    processed: list[dict] = []
    for m in all_markets:
        try:
            # Kalshi v2 uses *_dollars for already-decimal prices and *_fp
            # (fixed-point shares) for volume/open-interest. Newer responses
            # also still include the legacy *_cents fields on some objects.
            yes_p = _normalize_price(
                m.get("yes_ask_dollars")
                or m.get("yes_ask")
                or m.get("last_price_dollars")
                or m.get("last_price")
                or 0
            )
            no_p = _normalize_price(
                m.get("no_ask_dollars")
                or m.get("no_ask")
                or 0
            )
            if not no_p and yes_p:
                no_p = round(1.0 - yes_p, 4)

            volume_total = _to_float(m.get("volume_fp") or m.get("volume"))
            volume_24h = _to_float(m.get("volume_24h_fp") or m.get("volume_24h"))
            open_interest = _to_float(m.get("open_interest_fp") or m.get("open_interest"))

            processed.append({
                "ticker": m.get("ticker") or "",
                "title": m.get("title") or m.get("__event_title") or "",
                "subtitle": m.get("yes_sub_title") or m.get("subtitle") or "",
                "category": m.get("category") or m.get("__event_category") or "",
                "status": m.get("status") or "",
                "yes_price": yes_p,
                "no_price": no_p,
                "yes_bid": _normalize_price(
                    m.get("yes_bid_dollars") or m.get("yes_bid") or 0
                ),
                "yes_ask": _normalize_price(
                    m.get("yes_ask_dollars") or m.get("yes_ask") or 0
                ),
                "volume": volume_total,
                "volume_24h": volume_24h,
                "open_interest": open_interest,
                "liquidity_dollars": _to_float(m.get("liquidity_dollars")),
                "close_time": m.get("close_time") or "",
                "event_ticker": m.get("event_ticker") or m.get("__event_ticker") or "",
                "event_title": m.get("__event_title") or "",
            })
        except Exception:
            continue

    # Sort by 24h volume desc, with total volume as tiebreaker
    processed.sort(
        key=lambda x: (x.get("volume_24h") or 0, x.get("volume") or 0),
        reverse=True,
    )
    return processed[:limit]


# ─── RSA-PSS signing ────────────────────────────────────────────────────

def _load_rsa_key(pem: str):
    try:
        from cryptography.hazmat.primitives import serialization
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for Kalshi auth"
        ) from exc
    pem_bytes = pem.encode() if isinstance(pem, str) else pem
    return serialization.load_pem_private_key(pem_bytes, password=None)


def _sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    msg = (timestamp_ms + method.upper() + path).encode()
    signature = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


# ─── Authenticated client ───────────────────────────────────────────────

class KalshiClient:
    """Auth client using RSA-PSS request signing — read-only ops only."""

    def __init__(self, api_key: str, private_key_pem: str):
        self.api_key = api_key.strip()
        try:
            self.private_key = _load_rsa_key(private_key_pem)
        except Exception as e:
            raise RuntimeError(f"Invalid Kalshi private key: {e}") from e

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = _sign(self.private_key, ts, method, path)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "User-Agent": "PolymarketTopTraders/1.0",
        }

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        path = KALSHI_API_BASE + endpoint
        url = KALSHI_HOST + path
        try:
            with httpx.Client(timeout=15) as c:
                resp = c.get(url, params=params, headers=self._headers("GET", path))
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                except Exception:
                    err = {"raw": resp.text[:500]}
                return {"error": err, "status": resp.status_code}
            return resp.json() if resp.text else {}
        except httpx.HTTPError as e:
            log.warning("Kalshi GET %s failed: %s", endpoint, e)
            return {"error": str(e)}

    # ─── Read-only portfolio endpoints ──────────────────────────────────

    def get_balance(self) -> dict:
        return self._get("/portfolio/balance") or {}

    def get_positions(self) -> dict:
        return self._get("/portfolio/positions") or {}

    def get_fills(self, limit: int = 50) -> dict:
        return self._get("/portfolio/fills", params={"limit": limit}) or {}

    def get_orders(self, status: str = "resting") -> dict:
        return self._get("/portfolio/orders", params={"status": status}) or {}

    def test_connection(self) -> dict:
        result = self.get_balance()
        if isinstance(result, dict) and "error" not in result:
            return {"ok": True, "data": result}
        err = result.get("error") if isinstance(result, dict) else "unknown"
        return {"ok": False, "error": err}


# ─── Portfolio summary helper ────────────────────────────────────────────

def fetch_portfolio_summary(api_key: str, private_key_pem: str) -> dict:
    """One-call helper for the dashboard: balance + positions + fills + summary."""
    client = KalshiClient(api_key, private_key_pem)

    balance = client.get_balance()
    if "error" in balance:
        return {"ok": False, "error": balance.get("error"), "status": balance.get("status")}

    positions_resp = client.get_positions()
    if isinstance(positions_resp, dict) and "error" in positions_resp:
        return {"ok": False, "error": positions_resp.get("error"), "status": positions_resp.get("status")}

    fills_resp = client.get_fills(limit=50)
    if isinstance(fills_resp, dict) and "error" in fills_resp:
        return {"ok": False, "error": fills_resp.get("error"), "status": fills_resp.get("status")}

    market_pos = (positions_resp.get("market_positions") or []) if isinstance(positions_resp, dict) else []
    fills = (fills_resp.get("fills") or []) if isinstance(fills_resp, dict) else []

    # Aggregate metrics
    total_realized = sum((p.get("realized_pnl") or 0) for p in market_pos)
    total_unrealized = sum((p.get("market_exposure") or 0) for p in market_pos)
    open_positions = [p for p in market_pos if (p.get("position") or 0) != 0]
    total_position_count = len(open_positions)
    fee_paid = sum((p.get("fees_paid") or 0) for p in market_pos)

    # Normalize cents → dollars where Kalshi reports cents
    bal_cents = balance.get("balance") or 0
    bal_dollars = bal_cents / 100.0 if isinstance(bal_cents, (int, float)) else 0.0

    return {
        "ok": True,
        "balance": {
            "balance_cents": bal_cents,
            "balance_dollars": round(bal_dollars, 2),
        },
        "summary": {
            "open_positions": total_position_count,
            "realized_pnl_cents": total_realized,
            "realized_pnl_dollars": round(total_realized / 100.0, 2),
            "exposure_cents": total_unrealized,
            "exposure_dollars": round(total_unrealized / 100.0, 2),
            "fees_paid_cents": fee_paid,
            "fees_paid_dollars": round(fee_paid / 100.0, 2),
        },
        "positions": [
            {
                "ticker": p.get("ticker"),
                "market_title": p.get("market_title") or p.get("ticker"),
                "position": p.get("position") or 0,
                "side": "yes" if (p.get("position") or 0) > 0 else "no",
                "realized_pnl_dollars": round((p.get("realized_pnl") or 0) / 100.0, 2),
                "exposure_dollars": round((p.get("market_exposure") or 0) / 100.0, 2),
                "avg_cost_dollars": round((p.get("average_cost") or 0) / 100.0, 4),
                "fees_paid_dollars": round((p.get("fees_paid") or 0) / 100.0, 2),
            }
            for p in open_positions[:50]
        ],
        "fills": [
            {
                "ticker": f.get("ticker"),
                "side": f.get("side"),
                "action": f.get("action"),
                "count": f.get("count"),
                "price_cents": f.get("yes_price") or f.get("no_price") or 0,
                "created_time": f.get("created_time"),
            }
            for f in fills[:30]
        ],
    }

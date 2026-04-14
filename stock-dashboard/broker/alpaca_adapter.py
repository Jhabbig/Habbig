"""Alpaca broker adapter — supports paper and live trading via REST API.

Credentials dict shape::

    {
        "api_key": "PK...",
        "api_secret": "...",
        "paper": true          # optional, defaults to true
    }
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from .base import (
    Account, BrokerAdapter, Order, OrderType, Position, Quote, Side,
)

log = logging.getLogger("broker.alpaca")

LIVE_URL = "https://api.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


class AlpacaAdapter(BrokerAdapter):
    """Alpaca Markets adapter — equities, options, crypto."""

    BROKER_NAME = "alpaca"

    def __init__(self, credentials: dict) -> None:
        self._key = credentials["api_key"]
        self._secret = credentials["api_secret"]
        self._paper = credentials.get("paper", True)
        self._base = PAPER_URL if self._paper else LIVE_URL
        self._headers = {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json",
        }

    # ── Internal helpers ──────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        resp = requests.get(
            f"{self._base}{path}", headers=self._headers,
            params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self._base}{path}", headers=self._headers,
            json=payload, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        resp = requests.delete(
            f"{self._base}{path}", headers=self._headers, timeout=10,
        )
        resp.raise_for_status()

    def _data_get(self, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            f"{DATA_URL}{path}", headers=self._headers,
            params=params, timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Interface implementation ──────────────────────────────────────

    def test_connection(self) -> dict:
        try:
            acct = self._get("/v2/account")
            return {
                "ok": True,
                "account_id": acct.get("id", ""),
                "status": acct.get("status", ""),
                "paper": self._paper,
            }
        except requests.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_account(self) -> Account:
        acct = self._get("/v2/account")
        return Account(
            broker="alpaca",
            account_id=acct["id"],
            cash=float(acct.get("cash", 0)),
            portfolio_value=float(acct.get("portfolio_value", 0)),
            buying_power=float(acct.get("buying_power", 0)),
            currency=acct.get("currency", "USD"),
            paper=self._paper,
        )

    def get_positions(self) -> list[Position]:
        raw = self._get("/v2/positions")
        positions = []
        for p in raw:
            positions.append(Position(
                symbol=p["symbol"],
                qty=float(p["qty"]),
                avg_entry=float(p["avg_entry_price"]),
                current_price=float(p["current_price"]),
                market_value=float(p["market_value"]),
                unrealized_pnl=float(p["unrealized_pl"]),
                side=p.get("side", "long"),
            ))
        return positions

    def get_quote(self, symbol: str) -> Quote:
        data = self._data_get(f"/v2/stocks/{symbol.upper()}/quotes/latest")
        q = data.get("quote", {})
        return Quote(
            symbol=symbol.upper(),
            bid=float(q.get("bp", 0)),
            ask=float(q.get("ap", 0)),
            last=float(q.get("bp", 0)),  # Alpaca quotes don't have "last"; use bid
            volume=int(q.get("bs", 0) + q.get("as", 0)),
            timestamp=q.get("t", ""),
        )

    def place_order(
        self,
        symbol: str,
        qty: float,
        side: Side,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> Order:
        payload: dict = {
            "symbol": symbol.upper(),
            "qty": str(qty),
            "side": side.value,
            "type": order_type.value,
            "time_in_force": time_in_force,
        }
        if order_type == OrderType.LIMIT and limit_price is not None:
            payload["limit_price"] = str(round(limit_price, 2))

        raw = self._post("/v2/orders", payload)
        return self._parse_order(raw)

    def cancel_order(self, order_id: str) -> None:
        self._delete(f"/v2/orders/{order_id}")

    def get_orders(self, status: str = "open") -> list[Order]:
        raw = self._get("/v2/orders", params={"status": status, "limit": 50})
        return [self._parse_order(o) for o in raw]

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_order(raw: dict) -> Order:
        return Order(
            order_id=raw["id"],
            symbol=raw["symbol"],
            side=Side(raw["side"]),
            order_type=OrderType(raw["type"]) if raw.get("type") in ("market", "limit") else OrderType.MARKET,
            qty=float(raw.get("qty") or 0),
            status=raw.get("status", "unknown"),
            filled_qty=float(raw.get("filled_qty") or 0),
            filled_avg_price=float(raw.get("filled_avg_price") or 0),
            limit_price=float(raw["limit_price"]) if raw.get("limit_price") else None,
            created_at=raw.get("created_at", ""),
            broker="alpaca",
            raw=raw,
        )

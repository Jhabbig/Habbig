"""Polymarket CLOB client — trade execution via py-clob-client.

Also routes Kalshi-platform signals to the Kalshi REST API when live mode is on.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import aiohttp

from config import Config
from edge_calculator import Signal
from risk_manager import PositionSize

logger = logging.getLogger(__name__)


class TradingClient:
    """Wraps py-clob-client for order execution with paper trading support."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.paper_mode = self.config.PAPER_MODE
        self._client = None
        self._initialized = False
        self._kalshi_client = None
        self._kalshi_initialized = False

    def _init_kalshi_client(self) -> bool:
        if self._kalshi_initialized:
            return self._kalshi_client is not None
        self._kalshi_initialized = True
        if not self.config.KALSHI_ENABLED:
            logger.error("Kalshi live trading attempted but KALSHI_ENABLED=false")
            return False
        if not self.config.KALSHI_API_KEY_ID or not self.config.KALSHI_PRIVATE_KEY_PATH:
            logger.error("KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH required for live Kalshi trading")
            return False
        try:
            from kalshi_client import KalshiClient
            self._kalshi_client = KalshiClient(
                self.config.KALSHI_API_KEY_ID,
                self.config.KALSHI_PRIVATE_KEY_PATH,
            )
            logger.info("Live Kalshi client initialized")
            return True
        except Exception as e:
            logger.error("Failed to initialize Kalshi client: %s", e)
            return False

    def _init_client(self) -> bool:
        if self._initialized:
            return self._client is not None

        self._initialized = True

        if self.paper_mode:
            logger.info("Running in PAPER MODE — no live client needed")
            return True

        if not self.config.PRIVATE_KEY or not self.config.POLYMARKET_API_KEY:
            logger.error("PRIVATE_KEY and POLYMARKET_API_KEY required for live trading")
            return False

        try:
            from py_clob_client.client import ClobClient

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.config.PRIVATE_KEY,
                chain_id=137,
            )
            self._client.set_api_creds(self._client.create_or_derive_api_creds())
            logger.info("Live CLOB client initialized successfully")
            return True

        except ImportError:
            logger.error("py-clob-client not installed. Run: pip install py-clob-client")
            return False
        except Exception as e:
            logger.error("Failed to initialize CLOB client: %s", e)
            return False

    async def execute_trade(self, signal: Signal, position: PositionSize) -> dict:
        """Execute a trade (or simulate in paper mode)."""
        platform = getattr(signal.market, "platform", "polymarket")

        if platform == "polymarket" and not self._init_client():
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": "Polymarket client not initialized"}

        side = "YES" if signal.action == "BUY_YES" else "NO"
        price = signal.market_prob if signal.action == "BUY_YES" else (1.0 - signal.market_prob)
        token_id = signal.market.token_id if signal.action == "BUY_YES" else (signal.market.no_token_id or signal.market.token_id)

        if price <= 0:
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": "Invalid price"}

        shares = position.amount / price

        if self.paper_mode:
            return self._paper_trade(signal, position, side, price, shares)

        if platform == "kalshi":
            return await self._live_kalshi_trade(signal, position, side, price)

        return await self._live_trade(signal, position, side, price, shares, token_id)

    def _paper_trade(self, signal: Signal, position: PositionSize,
                     side: str, price: float, shares: float) -> dict:
        platform = getattr(signal.market, "platform", "polymarket")
        tag = platform.upper()
        logger.info(
            "[PAPER/%s] %s %s | %.1f shares @ $%.3f | Amount: $%.2f | Edge: %+.1f%% | %s",
            tag, signal.action, signal.market.question[:50],
            shares, price, position.amount, signal.edge * 100, signal.market.city,
        )
        return {
            "order_id": f"paper_{signal.market.condition_id[:8]}_{int(price*1000)}",
            "status": "filled", "fill_price": price,
            "amount": position.amount, "shares": shares,
            "side": side, "paper": True, "platform": platform,
        }

    async def _live_trade(self, signal: Signal, position: PositionSize,
                          side: str, price: float, shares: float, token_id: str) -> dict:
        if not self._client:
            return {"order_id": "", "status": "error", "error": "No CLOB client"}

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType

            order_args = OrderArgs(price=price, size=shares, side="BUY", token_id=token_id)
            logger.info("[LIVE] Placing FOK order: %s %s | %.1f shares @ $%.3f",
                        signal.action, side, shares, price)

            resp = self._client.create_and_post_order(order_args, OrderType.FOK)

            if resp and resp.get("success"):
                order_id = resp.get("orderID", "")
                logger.info("[LIVE] FOK order filled: %s", order_id)
                return {"order_id": order_id, "status": "filled", "fill_price": price,
                        "amount": position.amount, "shares": shares, "side": side, "paper": False}

            # FOK failed — try GTC with 2% slippage
            slippage_price = min(round(price * 1.02, 4), 0.99)
            shares = position.amount / slippage_price  # Recalculate shares at higher price to stay within position size
            order_args = OrderArgs(price=slippage_price, size=shares, side="BUY", token_id=token_id)
            logger.info("[LIVE] FOK failed, placing GTC @ $%.3f", slippage_price)

            resp = self._client.create_and_post_order(order_args, OrderType.GTC)

            if resp and resp.get("success"):
                return {"order_id": resp.get("orderID", ""), "status": "pending",
                        "fill_price": slippage_price, "amount": position.amount,
                        "shares": shares, "side": side, "paper": False}

            error = resp.get("errorMsg", "Unknown error") if resp else "No response"
            logger.error("[LIVE] Both FOK and GTC orders failed: %s", error)
            return {"order_id": "", "status": "error", "error": error, "amount": 0}

        except Exception as e:
            logger.error("[LIVE] Trade execution error: %s", e)
            return {"order_id": "", "status": "error", "error": str(e), "amount": 0}

    async def _live_kalshi_trade(self, signal: Signal, position: PositionSize,
                                 side: str, price: float) -> dict:
        """Place a real-money Kalshi limit order.

        Kalshi prices are integer cents (1-99), contracts are integer counts,
        each contract pays $1 if it resolves favorably. We cross the spread by
        1¢ as the limit cap so the order is marketable but bounded.
        """
        if not self._init_kalshi_client():
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": "Kalshi client not initialized"}

        capped_amount = min(position.amount, self.config.KALSHI_MAX_TRADE_SIZE)
        if capped_amount < position.amount:
            logger.info("[LIVE/KALSHI] Capped trade size $%.2f -> $%.2f (KALSHI_MAX_TRADE_SIZE)",
                        position.amount, capped_amount)

        price_cents = int(round(price * 100))
        if price_cents < 1 or price_cents > 99:
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": f"Invalid Kalshi price: {price_cents}c"}

        limit_cents = min(price_cents + 1, 99)
        unit_cost = limit_cents / 100.0
        count = int(capped_amount // unit_cost)
        if count < 1:
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": f"Position too small for Kalshi: ${capped_amount:.2f} at {limit_cents}c needs >=1 contract"}

        ticker = signal.market.condition_id
        kalshi_side = "yes" if side == "YES" else "no"
        coid = f"wbot_{ticker}_{int(time.time())}"[:64]

        logger.info(
            "[LIVE/KALSHI] %s %s | %d contracts @ %dc (cap %dc) | Budget: $%.2f | Edge: %+.1f%% | %s",
            signal.action, ticker, count, price_cents, limit_cents,
            capped_amount, signal.edge * 100, signal.market.city,
        )

        try:
            async with aiohttp.ClientSession() as session:
                resp = await self._kalshi_client.place_order(
                    session,
                    ticker=ticker,
                    side=kalshi_side,
                    action="buy",
                    count=count,
                    yes_price_cents=limit_cents if kalshi_side == "yes" else None,
                    no_price_cents=limit_cents if kalshi_side == "no" else None,
                    client_order_id=coid,
                )
        except Exception as e:
            logger.error("[LIVE/KALSHI] HTTP error: %s", e)
            return {"order_id": "", "status": "error", "error": str(e), "amount": 0}

        if "error" in resp:
            err = resp.get("error")
            logger.error("[LIVE/KALSHI] Order rejected: %s", err)
            return {"order_id": "", "status": "error",
                    "error": str(err), "amount": 0, "fill_price": 0}

        order = resp.get("order") or {}
        order_id = order.get("order_id", "")
        kalshi_status = order.get("status", "pending")
        # Kalshi returns "executed" for fully-filled, "resting" for unfilled, "canceled" if rejected
        normalized = "filled" if kalshi_status == "executed" else \
                     "error" if kalshi_status == "canceled" else "pending"
        fill_price = unit_cost
        actual_amount = count * unit_cost

        logger.info("[LIVE/KALSHI] Order %s: status=%s order_id=%s",
                    normalized, kalshi_status, order_id)

        return {
            "order_id": order_id,
            "status": normalized,
            "fill_price": fill_price,
            "amount": actual_amount,
            "shares": float(count),
            "side": side,
            "paper": False,
            "platform": "kalshi",
        }

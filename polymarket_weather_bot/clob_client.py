"""Polymarket CLOB client — trade execution via py-clob-client."""

from __future__ import annotations

import logging
from typing import Optional

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
        if not self._init_client():
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": "Client not initialized"}

        side = "YES" if signal.action == "BUY_YES" else "NO"
        price = signal.market_prob if signal.action == "BUY_YES" else (1.0 - signal.market_prob)
        token_id = signal.market.token_id if signal.action == "BUY_YES" else (signal.market.no_token_id or signal.market.token_id)

        if price <= 0:
            return {"order_id": "", "status": "error", "fill_price": 0, "amount": 0,
                    "error": "Invalid price"}

        shares = position.amount / price

        if self.paper_mode:
            return self._paper_trade(signal, position, side, price, shares)

        return await self._live_trade(signal, position, side, price, shares, token_id)

    def _paper_trade(self, signal: Signal, position: PositionSize,
                     side: str, price: float, shares: float) -> dict:
        logger.info(
            "[PAPER] %s %s | %.1f shares @ $%.3f | Amount: $%.2f | Edge: %+.1f%% | %s",
            signal.action, signal.market.question[:50],
            shares, price, position.amount, signal.edge * 100, signal.market.city,
        )
        return {
            "order_id": f"paper_{signal.market.condition_id[:8]}_{int(price*1000)}",
            "status": "filled", "fill_price": price,
            "amount": position.amount, "shares": shares,
            "side": side, "paper": True,
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

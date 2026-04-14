"""Abstract base class for stock broker adapters.

Every broker adapter implements this interface.  Dashboard code talks only
to these methods — never to a specific broker SDK.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


class Side(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class Account:
    broker: str
    account_id: str
    cash: float
    portfolio_value: float
    buying_power: float
    currency: str = "USD"
    paper: bool = False


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    side: str = "long"


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: int = 0
    timestamp: str = ""


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    limit_price: Optional[float] = None
    created_at: str = ""
    broker: str = ""
    raw: dict = field(default_factory=dict)


class BrokerAdapter(ABC):
    """Interface that every broker adapter must implement."""

    BROKER_NAME: str = ""

    @abstractmethod
    def __init__(self, credentials: dict) -> None:
        """Initialise the adapter with decrypted user credentials."""

    @abstractmethod
    def test_connection(self) -> dict:
        """Verify that credentials are valid.

        Returns ``{"ok": True}`` on success or ``{"ok": False, "error": "..."}``
        on failure.
        """

    @abstractmethod
    def get_account(self) -> Account:
        """Return account summary (cash, buying power, portfolio value)."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return all open positions."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Get a real-time quote for *symbol*."""

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        qty: float,
        side: Side,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
    ) -> Order:
        """Place an order.  Returns the submitted order."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order by ID."""

    @abstractmethod
    def get_orders(self, status: str = "open") -> list[Order]:
        """Return orders filtered by status (open / closed / all)."""

    # ── Convenience helpers (concrete) ────────────────────────────────

    def buy(self, symbol: str, qty: float,
            order_type: OrderType = OrderType.MARKET,
            limit_price: Optional[float] = None) -> Order:
        return self.place_order(symbol, qty, Side.BUY, order_type, limit_price)

    def sell(self, symbol: str, qty: float,
             order_type: OrderType = OrderType.MARKET,
             limit_price: Optional[float] = None) -> Order:
        return self.place_order(symbol, qty, Side.SELL, order_type, limit_price)

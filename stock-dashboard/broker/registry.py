"""Broker registry — maps platform names to adapter classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BrokerAdapter

# Platform name -> (module_path, class_name)
_REGISTRY: dict[str, tuple[str, str]] = {
    "alpaca": ("broker.alpaca_adapter", "AlpacaAdapter"),
    # Future:
    # "ibkr":       ("broker.ibkr_adapter",    "IBKRAdapter"),
    # "schwab":     ("broker.schwab_adapter",   "SchwabAdapter"),
    # "etrade":     ("broker.etrade_adapter",   "ETradeAdapter"),
    # "tradier":    ("broker.tradier_adapter",  "TradierAdapter"),
    # "tradestation": ("broker.tradestation_adapter", "TradeStationAdapter"),
    # "tastytrade": ("broker.tastytrade_adapter", "TastytradeAdapter"),
    # "saxo":       ("broker.saxo_adapter",     "SaxoAdapter"),
    # "ig":         ("broker.ig_adapter",       "IGAdapter"),
    # "oanda":      ("broker.oanda_adapter",    "OandaAdapter"),
}

SUPPORTED_BROKERS = list(_REGISTRY.keys())


def get_adapter(platform: str, credentials: dict) -> "BrokerAdapter":
    """Instantiate a broker adapter by platform name.

    Raises ``ValueError`` for unknown platforms.
    """
    if platform not in _REGISTRY:
        raise ValueError(
            f"Unknown broker platform '{platform}'. "
            f"Supported: {', '.join(SUPPORTED_BROKERS)}"
        )
    module_path, class_name = _REGISTRY[platform]
    import importlib
    mod = importlib.import_module(f".{module_path.split('.')[-1]}", package="broker")
    cls = getattr(mod, class_name)
    return cls(credentials)

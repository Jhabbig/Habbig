"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Polymarket credentials
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")

    # Kalshi credentials
    KALSHI_ENABLED: bool = os.getenv("KALSHI_ENABLED", "false").lower() == "true"
    KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
    KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    KALSHI_MAX_TRADE_SIZE: float = float(os.getenv("KALSHI_MAX_TRADE_SIZE", "100.0"))

    # Trading config
    PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
    EDGE_THRESHOLD: float = float(os.getenv("EDGE_THRESHOLD", "0.08"))
    BANKROLL: float = float(os.getenv("BANKROLL", "1000.0"))
    MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", "0.05"))
    DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.15"))
    MIN_LIQUIDITY: float = float(os.getenv("MIN_LIQUIDITY", "500.0"))
    MAX_FORECAST_HOURS: int = int(os.getenv("MAX_FORECAST_HOURS", "48"))
    RUN_INTERVAL_MINUTES: int = int(os.getenv("RUN_INTERVAL_MINUTES", "15"))

    # Logging / storage
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DB_PATH: str = os.getenv("DB_PATH", "trades.db")

    # Derived (computed in __init__ so env var overrides are applied)
    MAX_POSITION: float = 0.0
    DAILY_LOSS_LIMIT: float = 0.0

    def __init__(self):
        self.MAX_POSITION = self.BANKROLL * self.MAX_POSITION_PCT
        self.DAILY_LOSS_LIMIT = self.BANKROLL * self.DAILY_LOSS_LIMIT_PCT

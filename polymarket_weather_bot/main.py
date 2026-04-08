#!/usr/bin/env python3
"""Polymarket Weather Trading Bot — main entry point.

Runs a loop every 15 minutes:
1. Fetch active weather markets from Polymarket Gamma API
2. Parse market titles to extract city, date, temperature thresholds
3. Fetch weather forecasts from Open-Meteo (GFS ensemble)
4. Calculate probability vs market price using Gaussian model
5. Execute trades (paper or live) when edge exceeds threshold
"""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

from config import Config
from gamma_client import fetch_weather_markets, parse_weather_markets
from weather_client import get_forecast
from edge_calculator import calculate_edge, Signal
from risk_manager import RiskManager
from clob_client import TradingClient
from datastore import DataStore
from dashboard import print_run_summary, print_daily_report

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(Path(__file__).parent / "weather_bot.log", maxBytes=10*1024*1024, backupCount=5),
    ],
)
logger = logging.getLogger("main")


async def run_scan(
    config: Config,
    risk_mgr: RiskManager,
    trading_client: TradingClient,
    store: DataStore,
) -> None:
    """Run a single scan cycle: fetch markets → forecast → edge → trade."""
    now = datetime.now(timezone.utc)
    logger.info("Starting scan at %s", now.strftime("%Y-%m-%d %H:%M UTC"))

    async with aiohttp.ClientSession() as session:
        # Step 1: Fetch weather markets
        raw_markets = await fetch_weather_markets(session)
        if not raw_markets:
            logger.warning("No weather markets found")
            print_run_summary(0, [], 0, config)
            return

        # Step 2: Parse markets
        markets = parse_weather_markets(raw_markets)
        logger.info("Parsed %d weather market outcomes", len(markets))

        # Filter: only YES outcomes (avoid duplicate signals)
        markets = [m for m in markets if m.outcome == "Yes"]

        # Filter: only markets resolving within MAX_FORECAST_HOURS
        max_horizon = now + timedelta(hours=config.MAX_FORECAST_HOURS)
        filtered_markets = []
        for m in markets:
            if not m.target_date and not m.end_date:
                continue  # Skip markets with no date — can't forecast them
            if m.target_date and m.target_date <= max_horizon:
                filtered_markets.append(m)
            elif m.end_date and m.end_date <= max_horizon:
                filtered_markets.append(m)

        # Filter: minimum liquidity
        filtered_markets = [
            m for m in filtered_markets
            if m.liquidity >= config.MIN_LIQUIDITY or m.liquidity == 0
        ]

        logger.info("%d markets after filtering (horizon=%dh, min_liq=$%.0f)",
                     len(filtered_markets), config.MAX_FORECAST_HOURS, config.MIN_LIQUIDITY)

        # Step 3-6: For each market, get forecast and calculate edge
        signals = []
        trades_executed = 0

        for market in filtered_markets:
            if not market.target_date:
                continue

            forecast = await get_forecast(
                session, lat=market.lat, lon=market.lon,
                target_date=market.target_date,
                city=market.city, icao=market.station_icao,
            )

            if forecast is None:
                continue

            signal = calculate_edge(forecast, market, config.EDGE_THRESHOLD)
            signals.append(signal)
            store.log_signal(signal)

            if signal.action != "NO_TRADE":
                if risk_mgr.is_halted():
                    logger.warning("Trading halted — daily loss limit reached")
                    break

                position = risk_mgr.size_position(signal)
                if position.approved:
                    result = await trading_client.execute_trade(signal, position)

                    if result.get("status") in ("filled", "pending"):
                        risk_mgr.record_trade(market.condition_id, position.amount)
                        store.log_trade(signal, position, paper_mode=config.PAPER_MODE,
                                        order_id=result.get("order_id", ""),
                                        status=result["status"])
                        trades_executed += 1
                    else:
                        logger.warning("Trade failed: %s — %s",
                                       market.question[:50], result.get("error", "unknown"))

    print_run_summary(len(filtered_markets), signals, trades_executed, config)


async def main() -> None:
    """Main loop — run scan every RUN_INTERVAL_MINUTES."""
    config = Config()
    risk_mgr = RiskManager(config)
    trading_client = TradingClient(config)
    store = DataStore(config.DB_PATH)

    mode = "PAPER" if config.PAPER_MODE else "LIVE"
    logger.info("Weather bot starting in %s mode | Bankroll: $%.2f | Edge threshold: %.0f%%",
                mode, config.BANKROLL, config.EDGE_THRESHOLD * 100)

    current_day = datetime.now(timezone.utc).date()

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != current_day:
                logger.info("New day — resetting daily stats")
                risk_mgr.reset_daily()
                print_daily_report(store, config)
                current_day = today

            await run_scan(config, risk_mgr, trading_client, store)

        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
            print_daily_report(store, config)
            break
        except Exception as e:
            logger.error("Scan error: %s", e, exc_info=True)

        logger.info("Next scan in %d minutes", config.RUN_INTERVAL_MINUTES)
        try:
            await asyncio.sleep(config.RUN_INTERVAL_MINUTES * 60)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
            print_daily_report(store, config)
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

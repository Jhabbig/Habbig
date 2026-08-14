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
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone

import aiohttp

from config import Config
from gamma_client import fetch_weather_markets, fetch_market_resolutions, parse_weather_markets
from weather_client import get_forecast
from edge_calculator import calculate_edge, Signal
from risk_manager import RiskManager
from clob_client import TradingClient
from datastore import DataStore
from dashboard import print_run_summary, print_daily_report
from kalshi_markets import fetch_kalshi_weather_markets

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


async def settle_positions(
    session: aiohttp.ClientSession,
    risk_mgr: RiskManager,
    store: DataStore,
    open_signals: dict,
) -> None:
    """Settle open positions whose markets have resolved.

    This is what feeds RiskManager.daily_pnl — and therefore the daily loss
    circuit breaker — and the calibration table used for Brier scoring.
    Kalshi positions are skipped (no Gamma resolution data for them).
    """
    poly_ids = [
        cid for cid, sig in open_signals.items()
        if cid in risk_mgr.open_positions
        and getattr(sig.market, "platform", "polymarket") == "polymarket"
    ]
    if not poly_ids:
        return

    try:
        resolutions = await fetch_market_resolutions(session, poly_ids)
    except Exception as e:
        logger.warning("Settlement check failed: %s", e)
        return

    for cid, outcome in resolutions.items():
        sig = open_signals.pop(cid, None)
        if sig is None:
            continue
        amount = risk_mgr.open_positions.get(cid, 0.0)
        if sig.action == "BUY_YES":
            entry_price = sig.market_prob
            won = outcome == 1
        else:  # BUY_NO
            entry_price = 1.0 - sig.market_prob
            won = outcome == 0
        if won:
            pnl = amount * (1.0 / entry_price - 1.0) if entry_price > 0 else 0.0
        else:
            pnl = -amount

        store.log_calibration(sig, outcome)
        risk_mgr.record_pnl(pnl, cid)
        logger.info(
            "Settled '%s': outcome=%s pnl=$%+.2f (daily PnL: $%+.2f)",
            sig.market.question[:50], "YES" if outcome == 1 else "NO",
            pnl, risk_mgr.daily_pnl,
        )


async def run_scan(
    config: Config,
    risk_mgr: RiskManager,
    trading_client: TradingClient,
    store: DataStore,
    kalshi_client=None,
    open_signals: dict = None,
) -> None:
    """Run a single scan cycle: settle → fetch markets → forecast → edge → trade."""
    now = datetime.now(timezone.utc)
    logger.info("Starting scan at %s", now.strftime("%Y-%m-%d %H:%M UTC"))
    if open_signals is None:
        open_signals = {}

    async with aiohttp.ClientSession() as session:
        # Step 0: Settle any open positions whose markets have resolved,
        # so the daily loss limit reflects realized PnL before trading.
        await settle_positions(session, risk_mgr, store, open_signals)

        # Step 1a: Fetch Polymarket weather markets
        raw_markets = await fetch_weather_markets(session)
        markets = parse_weather_markets(raw_markets) if raw_markets else []
        logger.info("Polymarket: %d parsed market outcomes", len(markets))

        # Step 1b: Fetch Kalshi weather markets (if enabled)
        if config.KALSHI_ENABLED and kalshi_client is not None:
            try:
                kalshi_markets = await fetch_kalshi_weather_markets(
                    session, kalshi_client,
                )
                logger.info("Kalshi: %d weather markets", len(kalshi_markets))
                markets.extend(kalshi_markets)
            except Exception as e:
                logger.warning("Kalshi market fetch failed: %s", e)

        if not markets:
            logger.warning("No weather markets found on any platform")
            print_run_summary(0, [], 0, config)
            return

        # Filter: only YES outcomes (avoid duplicate signals)
        markets = [m for m in markets if m.outcome == "Yes"]

        # Filter: only markets resolving within MAX_FORECAST_HOURS, and not
        # already in the past — a parsed date more than a day old means the
        # outcome is already (or nearly) determined and there is no forecast
        # edge to trade, only stale "signals" on resolved markets.
        max_horizon = now + timedelta(hours=config.MAX_FORECAST_HOURS)
        min_horizon = now - timedelta(hours=24)
        filtered_markets = []
        for m in markets:
            if not m.target_date and not m.end_date:
                continue  # Skip markets with no date — can't forecast them
            if m.target_date and min_horizon <= m.target_date <= max_horizon:
                filtered_markets.append(m)
            elif m.end_date and min_horizon <= m.end_date <= max_horizon:
                filtered_markets.append(m)

        # Filter: minimum liquidity (skip for Kalshi — no liquidity data)
        filtered_markets = [
            m for m in filtered_markets
            if m.platform == "kalshi" or m.liquidity >= config.MIN_LIQUIDITY or m.liquidity == 0
        ]

        poly_count = sum(1 for m in filtered_markets if m.platform == "polymarket")
        kalshi_count = sum(1 for m in filtered_markets if m.platform == "kalshi")
        logger.info("%d markets after filtering (%d Polymarket, %d Kalshi, horizon=%dh)",
                     len(filtered_markets), poly_count, kalshi_count, config.MAX_FORECAST_HOURS)

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
                        open_signals[market.condition_id] = signal
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
    # Signals for currently open positions, keyed by condition_id, so
    # settle_positions can realize PnL and log calibration on resolution.
    open_signals: dict = {}

    # Initialize Kalshi client if enabled and credentials present
    kalshi_client = None
    if config.KALSHI_ENABLED and config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        try:
            from kalshi_client import KalshiClient
            kalshi_client = KalshiClient(config.KALSHI_API_KEY_ID, config.KALSHI_PRIVATE_KEY_PATH)
            logger.info("Kalshi client initialized (key: %s...)", config.KALSHI_API_KEY_ID[:8])
        except Exception as e:
            logger.warning("Failed to initialize Kalshi client: %s", e)
    elif config.KALSHI_ENABLED:
        logger.warning("KALSHI_ENABLED=true but credentials missing — Kalshi markets disabled")

    mode = "PAPER" if config.PAPER_MODE else "LIVE"
    platforms = "Polymarket" + (" + Kalshi" if kalshi_client else "")
    logger.info("Weather bot starting in %s mode | Platforms: %s | Bankroll: $%.2f | Edge threshold: %.0f%%",
                mode, platforms, config.BANKROLL, config.EDGE_THRESHOLD * 100)

    current_day = datetime.now(timezone.utc).date()

    while True:
        try:
            today = datetime.now(timezone.utc).date()
            if today != current_day:
                logger.info("New day — resetting daily stats")
                risk_mgr.reset_daily()
                print_daily_report(store, config)
                current_day = today

            await run_scan(config, risk_mgr, trading_client, store, kalshi_client,
                           open_signals=open_signals)

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

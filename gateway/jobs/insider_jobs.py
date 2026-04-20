"""Insider trading signal fetch, correlation, and resolution jobs.

Schedule:
  congressional trades: every 6 hours
  SEC Form 4: every 4 hours
  FEC campaign: daily
  correlate new signals: every 2 hours
  resolve insider correlations: every 6 hours (after market resolution)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from jobs.registry import register_job, register_cron

log = logging.getLogger("jobs.insider")


@register_job("fetch_congressional_trades")
async def fetch_congressional_trades() -> dict[str, Any]:
    """Fetch recent congressional stock trades from Capitol Trades API."""
    from insider.congressional_trades import CongressionalTradesFetcher
    from insider.base_fetcher import store_signals, update_fetcher_state

    fetcher = CongressionalTradesFetcher()
    if not fetcher.is_available():
        return {"fetched": 0, "error": "not configured"}

    try:
        signals = await fetcher.fetch()
        stored = store_signals(signals)
        update_fetcher_state("congressional_trades", stored)
        log.info("Congressional trades: fetched %d, stored %d new", len(signals), stored)

        # Trigger correlation for new signals
        if stored > 0:
            from jobs import enqueue_job
            await enqueue_job("correlate_insider_signals")

        return {"fetched": len(signals), "stored": stored}
    except Exception as e:
        log.exception("Congressional trades fetch failed: %s", e)
        update_fetcher_state("congressional_trades", 0, str(e))
        return {"fetched": 0, "error": str(e)}


@register_job("fetch_sec_form4")
async def fetch_sec_form4() -> dict[str, Any]:
    """Fetch recent SEC Form 4 insider filings from EDGAR."""
    from insider.sec_form4 import SECForm4Fetcher
    from insider.base_fetcher import store_signals, update_fetcher_state

    fetcher = SECForm4Fetcher()
    if not fetcher.is_available():
        return {"fetched": 0, "error": "not configured"}

    try:
        signals = await fetcher.fetch()
        stored = store_signals(signals)
        update_fetcher_state("sec_form4", stored)
        log.info("SEC Form 4: fetched %d, stored %d new", len(signals), stored)

        if stored > 0:
            from jobs import enqueue_job
            await enqueue_job("correlate_insider_signals")

        return {"fetched": len(signals), "stored": stored}
    except Exception as e:
        log.exception("SEC Form 4 fetch failed: %s", e)
        update_fetcher_state("sec_form4", 0, str(e))
        return {"fetched": 0, "error": str(e)}


@register_job("fetch_fec_campaign")
async def fetch_fec_campaign() -> dict[str, Any]:
    """Fetch FEC campaign finance data for donation surge detection."""
    from insider.fec_campaign import FECCampaignFetcher
    from insider.base_fetcher import store_signals, update_fetcher_state

    fetcher = FECCampaignFetcher()
    if not fetcher.is_available():
        return {"fetched": 0, "error": "not configured"}

    try:
        signals = await fetcher.fetch()
        stored = store_signals(signals)
        update_fetcher_state("fec_campaign", stored)
        log.info("FEC campaign: fetched %d, stored %d new", len(signals), stored)

        if stored > 0:
            from jobs import enqueue_job
            await enqueue_job("correlate_insider_signals")

        return {"fetched": len(signals), "stored": stored}
    except Exception as e:
        log.exception("FEC campaign fetch failed: %s", e)
        update_fetcher_state("fec_campaign", 0, str(e))
        return {"fetched": 0, "error": str(e)}


@register_job("correlate_insider_signals")
async def correlate_insider_signals() -> dict[str, Any]:
    """Correlate uncorrelated insider signals with active prediction markets.

    For each new signal: call Claude to find correlated markets, compute
    insider scores, store correlations, and send alerts for high-score ones.
    """
    import db
    from insider.correlator import correlate_signal_with_markets, store_correlations

    uncorrelated_ids = db.get_uncorrelated_signal_ids(limit=20)
    if not uncorrelated_ids:
        return {"correlated": 0, "reason": "no new signals"}

    # Fetch active markets
    try:
        from backend.markets.polymarket_client import PolymarketClient
        from backend.markets.kalshi_client import KalshiClient
        from backend.markets import unified_markets

        poly = PolymarketClient()
        kalshi = KalshiClient(
            base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
        )
        markets = await unified_markets.fetch_unified_markets(poly, kalshi, cache_ttl=300)
        await poly.close()
        await kalshi.close()
    except Exception as e:
        log.warning("Market fetch failed for correlation: %s", e)
        return {"correlated": 0, "error": str(e)}

    active = [m for m in markets if m.status == "active"]
    market_dicts = [m.to_dict() for m in active]

    total_correlated = 0
    alerts_sent = 0

    for signal_id in uncorrelated_ids:
        signal = db.get_insider_signal_by_id(signal_id)
        if not signal:
            continue

        signal_dict = dict(signal)
        signal_dict["id"] = signal_id

        try:
            correlations = await correlate_signal_with_markets(
                signal_dict, market_dicts, max_correlations=5
            )
            if correlations:
                stored = store_correlations(correlations)
                total_correlated += stored

                # Send alerts for high-score correlations
                threshold = float(os.environ.get("INSIDER_SCORE_ALERT_THRESHOLD", "0.6"))
                for corr in correlations:
                    if (corr.get("insider_score") or 0) >= threshold:
                        try:
                            await _send_insider_alert(signal_dict, corr)
                            alerts_sent += 1
                        except Exception as ae:
                            log.warning("Insider alert failed: %s", ae)

        except Exception as e:
            log.warning("Correlation failed for signal %d: %s", signal_id, e)

        # Rate limit Claude calls
        import asyncio
        await asyncio.sleep(1)

    return {"correlated": total_correlated, "signals_processed": len(uncorrelated_ids), "alerts_sent": alerts_sent}


@register_job("resolve_insider_correlations")
async def resolve_insider_correlations() -> dict[str, Any]:
    """Check if resolved markets matched insider signal predictions.

    Updates resolved_correct for each correlation where the market has settled.
    """
    import db

    with db.conn() as c:
        unresolved = c.execute(
            "SELECT c.id, c.market_id, c.implied_direction "
            "FROM insider_market_correlations c "
            "WHERE c.resolved = 0"
        ).fetchall()

    if not unresolved:
        return {"resolved": 0}

    resolved_count = 0
    for corr in unresolved:
        # Check if the market has been resolved in the predictions table
        with db.conn() as c:
            pred = c.execute(
                "SELECT resolved, resolved_correct, direction "
                "FROM predictions "
                "WHERE market_id = ? AND resolved = 1 LIMIT 1",
                (corr["market_id"],),
            ).fetchone()

        if not pred:
            continue

        # The market is resolved — check if insider implied direction was correct
        market_outcome_yes = bool(pred["resolved_correct"]) if pred["direction"] == "YES" else not bool(pred["resolved_correct"])
        insider_was_correct = (
            (corr["implied_direction"] == "YES" and market_outcome_yes) or
            (corr["implied_direction"] == "NO" and not market_outcome_yes)
        )

        with db.conn() as c:
            c.execute(
                "UPDATE insider_market_correlations SET resolved = 1, resolved_correct = ? WHERE id = ?",
                (1 if insider_was_correct else 0, corr["id"]),
            )
        resolved_count += 1

    return {"resolved": resolved_count, "checked": len(unresolved)}


async def _send_insider_alert(signal: dict, correlation: dict) -> None:
    """Send email alert for a high-score insider signal."""
    import db
    from jobs.email_jobs import enqueue_email

    app_url = os.environ.get("APP_URL", "https://narve.ai")

    # Find users who want insider alerts
    with db.conn() as c:
        users = c.execute(
            "SELECT id, email, username, insider_alert_threshold "
            "FROM users WHERE insider_alerts_enabled = 1 "
            "AND COALESCE(is_deleted, 0) = 0 "
            "AND COALESCE(email_unsubscribed_at, 0) = 0"
        ).fetchall()

    strength = signal.get("signal_strength", "weak")
    for user in users:
        threshold = user["insider_alert_threshold"] or "strong_only"
        if threshold == "strong_only" and strength != "strong":
            continue
        if threshold == "moderate_and_above" and strength == "weak":
            continue

        try:
            await enqueue_email(
                to=user["email"],
                template="insider_alert",
                context={
                    "app_url": app_url,
                    "signal_type": signal.get("signal_type", "").replace("_", " ").title(),
                    "source_name": signal.get("source_name", "Unknown"),
                    "action": signal.get("action", ""),
                    "asset": signal.get("asset_or_entity", ""),
                    "amount": f"${signal['amount_usd']:,.0f}" if signal.get("amount_usd") else "undisclosed",
                    "strength": strength.upper(),
                    "market_question": correlation.get("market_question", ""),
                    "implied_direction": correlation.get("implied_direction", ""),
                    "insider_score": f"{correlation.get('insider_score', 0):.0%}",
                    "explanation": correlation.get("correlation_explanation", ""),
                    "unsubscribe_url": f"{app_url}/unsubscribe?type=digest",
                },
                tags=["insider_alert"],
            )
        except Exception as e:
            log.warning("Insider alert email failed for user %d: %s", user["id"], e)


# ── Cron schedules ──────────────────────────────────────────────────────────

register_cron("fetch_congressional_trades", hour=0, minute=20)   # 00:20
register_cron("fetch_congressional_trades", hour=6, minute=20)   # 06:20
register_cron("fetch_congressional_trades", hour=12, minute=20)  # 12:20
register_cron("fetch_congressional_trades", hour=18, minute=20)  # 18:20

register_cron("fetch_sec_form4", hour=1, minute=40)              # 01:40
register_cron("fetch_sec_form4", hour=5, minute=40)              # 05:40
register_cron("fetch_sec_form4", hour=9, minute=40)              # 09:40
register_cron("fetch_sec_form4", hour=13, minute=40)             # 13:40
register_cron("fetch_sec_form4", hour=17, minute=40)             # 17:40
register_cron("fetch_sec_form4", hour=21, minute=40)             # 21:40

register_cron("fetch_fec_campaign", hour=3, minute=0)            # daily 03:00

register_cron("correlate_insider_signals", hour=2, minute=50)    # 02:50
register_cron("correlate_insider_signals", hour=8, minute=50)    # 08:50
register_cron("correlate_insider_signals", hour=14, minute=50)   # 14:50
register_cron("correlate_insider_signals", hour=20, minute=50)   # 20:50

register_cron("resolve_insider_correlations", hour=1, minute=0)  # 01:00
register_cron("resolve_insider_correlations", hour=7, minute=0)  # 07:00
register_cron("resolve_insider_correlations", hour=13, minute=0) # 13:00
register_cron("resolve_insider_correlations", hour=19, minute=0) # 19:00

"""Market resolution auto-detection (F2).

Polls Polymarket and Kalshi APIs for settled markets, matches them to
predictions in the DB, marks predictions as resolved (correct/incorrect),
then triggers credibility recomputation and user notifications.

Runs as a cron job every hour at :17.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from jobs.registry import register_job, register_cron


log = logging.getLogger("jobs.resolution")


@register_job("poll_market_resolutions")
async def poll_market_resolutions() -> dict[str, Any]:
    """Check all unresolved prediction markets for settlement.

    For each unresolved market_id in the predictions table:
      - If poly:{slug} → fetch via Polymarket Gamma API, check for resolution
      - If kalshi:{ticker} → fetch via Kalshi API, check for settlement
    When a market is resolved, mark all matching predictions as correct/incorrect
    and enqueue follow-up jobs (credibility recompute, notifications).
    """
    import db
    from jobs import enqueue_job
    from jobs.quiet_hours import _within_quiet_hours

    # Audit HIGHx4: data pass (poll APIs, resolve predictions, fire
    # credibility recompute) ALWAYS runs so the public leaderboards and
    # credibility scores are not stalled by the quiet window. Only the
    # user-facing notification fan-out is gated; the resolution itself
    # is persisted, so the next non-quiet tick can pick the email/push
    # work back up via send_market_resolution_notifications driven off
    # ``user_market_views.notified_on_resolution = 0``.
    notifications_gated = _within_quiet_hours()

    market_ids = db.get_unresolved_market_ids()
    if not market_ids:
        return {"resolved_predictions": 0, "markets_checked": 0, "detail": "no unresolved markets"}

    # Lazy-import clients so this module doesn't crash if the market
    # packages are missing (dev environments without API keys).
    try:
        from backend.markets.polymarket_client import PolymarketClient
        from backend.markets.kalshi_client import KalshiClient
    except ImportError as e:
        log.warning("Market client import failed, skipping resolution poll: %s", e)
        return {"resolved_predictions": 0, "markets_checked": 0, "error": str(e)}

    poly = PolymarketClient(
        gamma_base=os.environ.get("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"),
        clob_base=os.environ.get("POLYMARKET_CLOB_API", "https://clob.polymarket.com"),
    )
    kalshi = KalshiClient(
        base_url=os.environ.get("KALSHI_API_BASE", "https://trading-api.kalshi.com/trade-api/v2"),
    )

    resolved_total = 0
    errors = 0

    for market_id in market_ids:
        try:
            if market_id.startswith("poly:"):
                slug = market_id[5:]
                raw = await poly.get_market(slug)
                if not raw:
                    continue
                # Polymarket resolved markets have `resolved: True` and `outcome: str`
                is_resolved = raw.get("resolved") or raw.get("closed")
                if not is_resolved:
                    continue
                outcome_str = (raw.get("outcome") or "").upper()
                # Polymarket outcomes: "Yes", "No", or resolved prices
                resolved_prices = raw.get("outcomePrices")
                if outcome_str in ("YES", "1"):
                    outcome_yes = True
                elif outcome_str in ("NO", "0"):
                    outcome_yes = False
                elif resolved_prices:
                    # Polymarket Gamma returns `outcomePrices` as a JSON-encoded
                    # string like "[\"0.65\",\"0.35\"]". Historically we fell
                    # back to eval() for the string case — that was an RCE
                    # primitive if Polymarket ever served (or was MITM'd into
                    # serving) a crafted payload, since eval runs arbitrary
                    # Python. json.loads is the correct parser: it rejects any
                    # non-JSON input with ValueError, which the except clause
                    # below already handles.
                    try:
                        if isinstance(resolved_prices, list):
                            prices = resolved_prices
                        else:
                            prices = json.loads(resolved_prices)
                        outcome_yes = float(prices[0]) > 0.5
                    except (ValueError, TypeError, IndexError):
                        log.warning("Could not parse outcome prices for %s: %s", market_id, resolved_prices)
                        continue
                else:
                    continue

                count = db.resolve_predictions_for_market(market_id, outcome_yes)
                if count > 0:
                    resolved_total += count
                    log.info("Resolved %d predictions for %s (outcome=%s)",
                             count, market_id, "YES" if outcome_yes else "NO")
                    # Flush cached reads that reflect an unresolved market.
                    try:
                        from cache import ttl_invalidate
                        ttl_invalidate.on_market_resolved(slug)
                        ttl_invalidate.on_market_resolved(market_id)
                    except Exception as ce:
                        log.warning("ttl_invalidate on_market_resolved failed for %s: %s", market_id, ce)
                    # Enqueue notification — skipped inside the quiet
                    # window. user_market_views.notified_on_resolution
                    # stays 0 for affected viewers, so a later non-quiet
                    # tick of send_market_resolution_notifications still
                    # delivers (it scans across markets, not just newly-
                    # resolved ones).
                    if not notifications_gated:
                        try:
                            await enqueue_job(
                                "send_market_resolution_notifications",
                                market_slug=slug,
                                outcome="YES" if outcome_yes else "NO",
                                market_question=raw.get("question", slug),
                            )
                        except Exception as ne:
                            log.warning("Failed to enqueue notification for %s: %s", market_id, ne)

            elif market_id.startswith("kalshi:"):
                ticker = market_id[7:]
                raw = await kalshi.get_market(ticker)
                if not raw:
                    continue
                status = (raw.get("status") or raw.get("result_status") or "").lower()
                if status not in ("settled", "finalized", "closed"):
                    continue
                result = (raw.get("result") or raw.get("outcome") or "").lower()
                if result == "yes":
                    outcome_yes = True
                elif result == "no":
                    outcome_yes = False
                elif "yes_price" in raw and raw.get("yes_price") is not None:
                    # Settled price: 1.0 = YES, 0.0 = NO
                    outcome_yes = float(raw["yes_price"]) > 0.5
                else:
                    continue

                count = db.resolve_predictions_for_market(market_id, outcome_yes)
                if count > 0:
                    resolved_total += count
                    log.info("Resolved %d predictions for %s (outcome=%s)",
                             count, market_id, "YES" if outcome_yes else "NO")
                    try:
                        from cache import ttl_invalidate
                        ttl_invalidate.on_market_resolved(ticker)
                        ttl_invalidate.on_market_resolved(market_id)
                    except Exception as ce:
                        log.warning("ttl_invalidate on_market_resolved failed for %s: %s", market_id, ce)
                    if not notifications_gated:
                        try:
                            await enqueue_job(
                                "send_market_resolution_notifications",
                                market_slug=ticker,
                                outcome="YES" if outcome_yes else "NO",
                                market_question=raw.get("title", ticker),
                            )
                        except Exception as ne:
                            log.warning("Failed to enqueue notification for %s: %s", market_id, ne)

        except Exception as e:
            log.exception("Error polling resolution for %s: %s", market_id, e)
            errors += 1

    # Close HTTP clients
    try:
        await poly.close()
    except Exception:
        pass
    try:
        await kalshi.close()
    except Exception:
        pass

    # Trigger credibility recomputation if any predictions were resolved
    if resolved_total > 0:
        try:
            await enqueue_job("recompute_credibilities")
        except Exception as e:
            log.warning("Failed to enqueue credibility recompute: %s", e)

    return {
        "resolved_predictions": resolved_total,
        "markets_checked": len(market_ids),
        "errors": errors,
    }


@register_job("generate_resolution_retrospective")
async def generate_resolution_retrospective(
    market_id: str = "",
    outcome: str = "",
    market_question: str = "",
) -> dict[str, Any]:
    """Generate a Claude-powered retrospective for a resolved market (F6).

    Called automatically after resolution detection, or on-demand from admin.
    """
    if not market_id or not outcome:
        return {"error": "market_id and outcome required"}

    try:
        from intelligence.retrospective import generate_retrospective
        result = await generate_retrospective(market_id, outcome, market_question)
        log.info("Retrospective generated for %s: %d chars",
                 market_id, len(result.get("analysis_text", "")))
        return result
    except Exception as e:
        log.exception("Retrospective generation failed for %s: %s", market_id, e)
        return {"error": str(e)}


# Run every hour at :17
register_cron("poll_market_resolutions", minute=17)

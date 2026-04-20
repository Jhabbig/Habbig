"""Insider signal → prediction market correlation engine.

Uses Claude to find connections between insider trading activity and
active prediction market outcomes. Keyword matching is too brittle —
Claude understands the causal chains.

Example correlations:
  - Senator buys Raytheon → "Will US increase defence spending?" market
  - Executive sells pharma stock → "Will FDA approve drug X?" market
  - Large FEC donation to candidate → election market for that candidate

Follows the intelligence/retrospective.py pattern:
  cache-first → Claude call → parse JSON → store → stub on failure
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("insider.correlator")

CORRELATOR_MODEL = os.environ.get("CORRELATOR_MODEL", "claude-haiku-4-5-20250929")
CORRELATOR_MAX_TOKENS = 2048

CORRELATOR_SYSTEM_PROMPT = """\
You are a financial intelligence analyst finding connections between insider
trading activity and prediction market outcomes.

You will receive:
1. An insider trading signal (who traded what, how much, when)
2. A list of active prediction markets (title, category, current YES price)

Identify which markets are most likely affected by this insider signal.
Explain why, and predict which direction the signal implies.

Rules:
- Only flag correlations with a genuine logical connection
- "direct": market outcome directly follows from what the insider knows
- "indirect": signal implies sector/macro conditions affecting the market
- "sector": signal implies industry conditions relevant to the market
- "political": congressional trade implies how they'll vote on legislation

Confidence levels:
- "high": clear direct connection, large amount, recent filing, relevant committee
- "medium": plausible connection, moderate amount or delayed filing
- "low": speculative connection, could be coincidence

Be honest about uncertainty. Flag stretches.

Respond with ONLY a JSON array of correlations:
[
  {
    "market_index": 0,
    "correlation_type": "direct",
    "explanation": "Armed Services Committee member buying defence contractor stock suggests knowledge of upcoming budget approval.",
    "implied_direction": "YES",
    "confidence": "high"
  }
]

If no correlations exist, respond with: []
"""


def _build_signal_description(signal: dict) -> str:
    """Build a human-readable description of an insider signal for Claude."""
    parts = [
        f"Signal type: {signal.get('signal_type', 'unknown')}",
        f"Person: {signal.get('source_name', 'Unknown')} ({signal.get('source_type', '')})",
        f"Action: {signal.get('action', 'unknown')}",
        f"Asset: {signal.get('asset_or_entity', 'unknown')}",
    ]
    if signal.get("amount_usd"):
        parts.append(f"Amount: ${signal['amount_usd']:,.0f}")
    if signal.get("committee"):
        parts.append(f"Committee: {signal['committee']}")
    if signal.get("party"):
        parts.append(f"Party: {signal['party']}")
    if signal.get("delay_days") is not None:
        parts.append(f"Disclosure delay: {signal['delay_days']} days")
    parts.append(f"Signal strength: {signal.get('signal_strength', 'unknown')}")
    return "\n".join(parts)


def _build_markets_description(markets: list[dict]) -> str:
    """Build a compact list of markets for Claude."""
    lines = []
    for i, m in enumerate(markets[:30]):  # cap for token budget
        title = (m.get("title") or "")[:100]
        cat = m.get("category", "")
        price = m.get("yes_price", 0)
        lines.append(f"[{i}] {title} (category: {cat}, YES: {price:.0%})")
    return "\n".join(lines)


async def correlate_signal_with_markets(
    signal: dict,
    markets: list[dict],
    max_correlations: int = 5,
) -> list[dict]:
    """Find prediction markets correlated with an insider signal.

    Uses Claude to perform the correlation analysis. Falls back to
    empty list on failure (never crashes the pipeline).

    Returns list of correlation dicts ready for DB storage.
    """
    if not markets:
        return []

    signal_desc = _build_signal_description(signal)
    markets_desc = _build_markets_description(markets)

    user_message = (
        f"INSIDER SIGNAL:\n{signal_desc}\n\n"
        f"ACTIVE PREDICTION MARKETS:\n{markets_desc}"
    )

    response_text = await _call_claude(user_message)
    if response_text is None:
        return []

    # Parse Claude's response
    try:
        correlations = json.loads(response_text)
        if not isinstance(correlations, list):
            correlations = []
    except (json.JSONDecodeError, TypeError):
        log.warning("Correlator response not valid JSON")
        return []

    # Build correlation records
    now = int(time.time())
    results = []

    for corr in correlations[:max_correlations]:
        try:
            market_idx = corr.get("market_index", -1)
            if market_idx < 0 or market_idx >= len(markets):
                continue

            market = markets[market_idx]
            implied_dir = (corr.get("implied_direction") or "").upper()
            if implied_dir not in ("YES", "NO"):
                continue

            confidence = corr.get("confidence", "low")
            market_price = market.get("yes_price", 0.5)

            # Compute insider score
            score = compute_insider_score(
                signal_strength=signal.get("signal_strength", "weak"),
                delay_days=signal.get("delay_days"),
                amount_usd=signal.get("amount_usd"),
                correlation_confidence=confidence,
            )

            results.append({
                "signal_id": signal.get("id"),
                "market_id": market.get("id", ""),
                "market_question": (market.get("title") or "")[:200],
                "correlation_type": corr.get("correlation_type", "indirect"),
                "correlation_explanation": (corr.get("explanation") or "")[:500],
                "implied_direction": implied_dir,
                "implied_confidence": confidence,
                "market_price_at_detection": market_price,
                "insider_score": round(score, 4),
                "detected_at": now,
            })
        except Exception as e:
            log.warning("Failed to process correlation: %s", e)

    return results


def compute_insider_score(
    signal_strength: str = "weak",
    delay_days: Optional[int] = None,
    amount_usd: Optional[float] = None,
    correlation_confidence: str = "low",
) -> float:
    """Composite score 0.0-1.0 for how actionable an insider signal is.

    Components (weighted):
      1. Signal strength (0.4): strong=1.0, moderate=0.6, weak=0.3
      2. Disclosure delay (0.2): 0-5d=1.0, 6-15d=0.7, 16-30d=0.4, >30d=0.1
      3. Amount significance (0.2): >$1M=1.0, $500k=0.8, $100k=0.6, $50k=0.4, <$50k=0.2
      4. Correlation confidence (0.2): high=1.0, medium=0.6, low=0.3
    """
    # 1. Signal strength
    strength_map = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}
    w_strength = strength_map.get(signal_strength, 0.3)

    # 2. Delay
    delay = delay_days if delay_days is not None else 999
    if delay <= 5:
        w_delay = 1.0
    elif delay <= 15:
        w_delay = 0.7
    elif delay <= 30:
        w_delay = 0.4
    else:
        w_delay = 0.1

    # 3. Amount
    amount = amount_usd or 0
    if amount >= 1_000_000:
        w_amount = 1.0
    elif amount >= 500_000:
        w_amount = 0.8
    elif amount >= 100_000:
        w_amount = 0.6
    elif amount >= 50_000:
        w_amount = 0.4
    else:
        w_amount = 0.2

    # 4. Correlation confidence
    conf_map = {"high": 1.0, "medium": 0.6, "low": 0.3}
    w_conf = conf_map.get(correlation_confidence, 0.3)

    score = (w_strength * 0.4) + (w_delay * 0.2) + (w_amount * 0.2) + (w_conf * 0.2)
    return max(0.0, min(1.0, score))


def store_correlations(correlations: list[dict]) -> int:
    """Store insider-market correlations in the database."""
    import db

    stored = 0
    for corr in correlations:
        try:
            with db.conn() as c:
                c.execute(
                    """INSERT INTO insider_market_correlations
                        (signal_id, market_id, market_question, correlation_type,
                         correlation_explanation, implied_direction, implied_confidence,
                         market_price_at_detection, insider_score, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        corr["signal_id"],
                        corr["market_id"],
                        corr.get("market_question", ""),
                        corr.get("correlation_type", "indirect"),
                        corr.get("correlation_explanation", ""),
                        corr.get("implied_direction", ""),
                        corr.get("implied_confidence", "low"),
                        corr.get("market_price_at_detection"),
                        corr.get("insider_score"),
                        corr.get("detected_at", int(time.time())),
                    ),
                )
                stored += 1
        except Exception as e:
            log.warning("Failed to store correlation: %s", e)

    return stored


async def _call_claude(user_message: str) -> Optional[str]:
    """Call Claude for correlation analysis."""
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed, skipping correlation")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CORRELATOR_MODEL,
            max_tokens=CORRELATOR_MAX_TOKENS,
            system=CORRELATOR_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text if response.content else None
    except Exception as exc:
        log.exception("Claude correlator call failed: %s", exc)
        return None

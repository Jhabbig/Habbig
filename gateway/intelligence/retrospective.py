"""Post-resolution retrospective analysis (F6).

When a market resolves, this module generates a Claude-powered retrospective
analysing how narve.ai's credibility-weighted intelligence performed.

Follows the same lazy-generation, cache-first, stub-on-failure pattern as
intelligence/environmental.py.

Output:
  - Which sources called the outcome correctly (and how early)
  - Which sources were wrong
  - How the betyc consensus compared to the market price
  - Plain-English narrative suitable for email and in-app display
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("intelligence.retrospective")

RETROSPECTIVE_MODEL = os.environ.get(
    "RETROSPECTIVE_MODEL", "claude-haiku-4-5-20250929"
)
RETROSPECTIVE_MAX_TOKENS = 1024


SYSTEM_PROMPT = """\
You are an analyst reviewing how prediction market intelligence performed
after a market resolved. You will receive:
  - The market question and its outcome (YES or NO)
  - A list of predictions from various sources, each with:
    - source handle, credibility score, direction (YES/NO), predicted probability
    - whether the prediction was correct

Your task: write a concise retrospective (150-250 words) that:
1. States the outcome clearly.
2. Summarises the narve.ai consensus vs the market price.
3. Highlights the top sources who called it correctly (especially if early or contrarian).
4. Notes which sources were wrong.
5. Concludes with what this tells us about source credibility going forward.

Also return structured JSON with:
- "analysis": the narrative text
- "correct_sources": [{handle, credibility, days_early}]
- "wrong_sources": [{handle, credibility}]

Respond ONLY with valid JSON matching this schema. No markdown, no preamble.
"""


def _build_user_message(
    market_question: str,
    outcome: str,
    betyc_consensus: Optional[float],
    market_price: Optional[float],
    predictions: list[dict],
) -> str:
    """Build the user message for Claude."""
    pred_lines = []
    for p in predictions[:20]:  # cap to avoid token blow-up
        pred_lines.append(
            f"- @{p.get('source_handle', '?')} "
            f"(credibility: {p.get('global_credibility', '?')}) "
            f"predicted {p.get('direction', '?')} "
            f"(prob: {p.get('predicted_probability', '?')}) "
            f"→ {'CORRECT' if p.get('resolved_correct') else 'WRONG'}"
        )
    return (
        f"Market: {market_question}\n"
        f"Outcome: {outcome}\n"
        f"narve.ai consensus: {betyc_consensus}\n"
        f"Market price at time of predictions: {market_price}\n\n"
        f"Predictions:\n" + "\n".join(pred_lines)
    )


async def generate_retrospective(
    market_id: str,
    outcome: str,
    market_question: str,
) -> dict[str, Any]:
    """Generate a retrospective analysis for a resolved market.

    Caches in the resolution_retrospectives table. Returns the retrospective
    dict or a stub if generation fails.
    """
    import db

    # Check cache first
    existing = _get_cached(market_id)
    if existing:
        return existing

    # Gather prediction data
    preds = db.get_predictions_for_market(market_id)
    if not preds:
        return _stub(market_id, outcome, market_question, "No predictions found")

    pred_dicts = [
        {
            "source_handle": p["source_handle"],
            "global_credibility": p["global_credibility"],
            "direction": p["direction"],
            "predicted_probability": p["predicted_probability"],
            "resolved_correct": p["resolved_correct"],
            "extracted_at": p["extracted_at"],
        }
        for p in preds
    ]

    # Compute what betyc consensus was
    betyc_result = db.calculate_betyc_probability(pred_dicts)
    betyc_consensus = betyc_result.get("betyc_yes_probability")

    # Get the last market snapshot for price context
    slug = market_id.split(":", 1)[1] if ":" in market_id else market_id
    snap = db.get_latest_market_snapshot(slug)
    market_price = snap["yes_price"] if snap else None

    # Call Claude
    user_msg = _build_user_message(
        market_question, outcome, betyc_consensus, market_price, pred_dicts
    )
    analysis = await _call_claude(user_msg)

    if analysis is None:
        return _stub(market_id, outcome, market_question, "Claude API call failed")

    # Parse response
    try:
        parsed = json.loads(analysis)
    except (json.JSONDecodeError, TypeError):
        parsed = {"analysis": analysis, "correct_sources": [], "wrong_sources": []}

    result = {
        "market_id": market_id,
        "market_question": market_question,
        "outcome": outcome,
        "betyc_consensus_was": betyc_consensus,
        "market_price_was": market_price,
        "edge_was": round(betyc_consensus - market_price, 4) if betyc_consensus and market_price else None,
        "analysis_text": parsed.get("analysis", ""),
        "top_correct_sources": json.dumps(parsed.get("correct_sources", [])),
        "top_wrong_sources": json.dumps(parsed.get("wrong_sources", [])),
        "prediction_count": len(pred_dicts),
        "generated_by": RETROSPECTIVE_MODEL,
    }

    # Cache in DB
    _store(result)
    return result


async def _call_claude(user_message: str) -> Optional[str]:
    """Call Claude to generate the retrospective. Returns raw response text."""
    from ai import client as _ai_client
    return await _ai_client.call_claude(
        feature="retrospective",
        system=SYSTEM_PROMPT,
        user=user_message,
        model=RETROSPECTIVE_MODEL,
        max_tokens=RETROSPECTIVE_MAX_TOKENS,
    )


def _get_cached(market_id: str) -> Optional[dict]:
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT * FROM resolution_retrospectives WHERE market_id = ?",
            (market_id,),
        ).fetchone()
    if row:
        return dict(row)
    return None


def _store(result: dict) -> None:
    import db
    now = int(time.time())
    with db.conn() as c:
        c.execute(
            """INSERT INTO resolution_retrospectives
                (market_id, market_question, outcome, betyc_consensus_was,
                 market_price_was, edge_was, analysis_text, top_correct_sources,
                 top_wrong_sources, prediction_count, generated_at, generated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                analysis_text = excluded.analysis_text,
                top_correct_sources = excluded.top_correct_sources,
                top_wrong_sources = excluded.top_wrong_sources,
                generated_at = excluded.generated_at
            """,
            (
                result["market_id"],
                result["market_question"],
                result["outcome"],
                result.get("betyc_consensus_was"),
                result.get("market_price_was"),
                result.get("edge_was"),
                result["analysis_text"],
                result.get("top_correct_sources", "[]"),
                result.get("top_wrong_sources", "[]"),
                result.get("prediction_count", 0),
                now,
                result.get("generated_by", RETROSPECTIVE_MODEL),
            ),
        )


def _stub(market_id: str, outcome: str, question: str, reason: str) -> dict:
    return {
        "market_id": market_id,
        "market_question": question,
        "outcome": outcome,
        "analysis_text": f"Retrospective unavailable: {reason}",
        "top_correct_sources": "[]",
        "top_wrong_sources": "[]",
        "prediction_count": 0,
        "generated_by": "stub",
    }

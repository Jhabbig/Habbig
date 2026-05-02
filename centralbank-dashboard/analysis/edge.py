"""Edge calculation: implied probability vs Polymarket YES price.

Convention:
    edge = implied_prob - polymarket_yes_price

  edge > 0  → Polymarket underprices our outcome → BUY YES
  edge < 0  → Polymarket overprices our outcome  → SELL YES (or buy NO)

The threshold for surfacing a "trade" signal is configurable; default is 3
percentage points to filter out noise (Polymarket's bid-ask is often ~1-2 pp
on liquid markets, plus our implied number has its own modeling error).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ingestion import decision_calendar, implied_path, polymarket_client

# Edge threshold (absolute, in probability points) above which we tag a direction.
# 3 pp is conservative — Polymarket's spread + our own modelling slack live below this.
EDGE_THRESHOLD = 0.03


def _direction(edge: float) -> str:
    if edge > EDGE_THRESHOLD:
        return "BUY YES"
    if edge < -EDGE_THRESHOLD:
        return "SELL YES"
    return "—"


def compute() -> dict:
    """Build the edge view for the next FOMC. Always returns a dict; missing
    pieces are reported in `errors` rather than raised."""
    today = datetime.now(timezone.utc).date()
    cal = decision_calendar.upcoming(today, horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        return {"as_of": today.isoformat(), "errors": ["no upcoming FOMC in horizon"]}

    meeting_date = date.fromisoformat(fomc["decision_date"])

    implied = implied_path.get_cached()
    probs = implied.get("probabilities", {}) or {}

    markets = polymarket_client.get_cached_for_meeting(meeting_date)

    # If we have no implied numbers at all, surface market prices but mark
    # implied/edge/direction as unknown — never fabricate a "0% implied" that
    # would generate a fake SELL YES signal on every market.
    have_implied = bool(probs)

    rows: list[dict] = []
    for m in markets:
        bucket = m["outcome_bucket"]
        if have_implied:
            # Bucket not in `probs` means our linear-interp model assigned it
            # ~0 — that's a real signal, not a missing reading. Treat as 0.0.
            implied_p = float(probs.get(bucket, 0.0))
            edge = implied_p - m["polymarket_price"]
            rows.append({
                "outcome_bucket": bucket,
                "question": m["question"],
                "polymarket_price": m["polymarket_price"],
                "implied_prob": round(implied_p, 4),
                "edge": round(edge, 4),
                "edge_pp": round(edge * 100, 1),
                "direction": _direction(edge),
                "volume_24h": m["volume_24h"],
                "url": m.get("url"),
                "end_date": m.get("end_date"),
            })
        else:
            rows.append({
                "outcome_bucket": bucket,
                "question": m["question"],
                "polymarket_price": m["polymarket_price"],
                "implied_prob": None,
                "edge": None,
                "edge_pp": None,
                "direction": "—",
                "volume_24h": m["volume_24h"],
                "url": m.get("url"),
                "end_date": m.get("end_date"),
            })

    # Sort: rows with edge by |edge| desc, then unknown-edge rows by volume desc.
    rows.sort(key=lambda r: (
        1 if r["edge"] is None else 0,            # edge-known rows first
        -abs(r["edge"]) if r["edge"] is not None else 0,
        -(r["volume_24h"] or 0),
    ))

    errors: list[str] = []
    if not probs:
        errors.append("implied probabilities unavailable (futures or rate data missing)")
    if not markets:
        errors.append("no Polymarket markets matched (or API unreachable)")

    return {
        "as_of": today.isoformat(),
        "meeting": fomc,
        "implied_probabilities": probs,
        "edge_threshold_pp": round(EDGE_THRESHOLD * 100, 1),
        "rows": rows,
        "errors": errors,
    }


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compute(), indent=2))

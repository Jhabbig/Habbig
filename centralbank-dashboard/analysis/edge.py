"""Edge calculation — implied probability vs Polymarket YES vs Kalshi YES.

Two flavours of edge for each FOMC outcome bucket (cut25, hold, hike25, …):

  * **Implied edge**  — does the prediction-market price match our model?
        edge_implied = implied_prob - venue_yes_price
        > 0  → market underprices → BUY YES
        < 0  → market overprices  → SELL YES

  * **Cross-venue arb** — do Polymarket and Kalshi agree on the same outcome?
        spread = polymarket_price - kalshi_price
        |spread| > 3 pp  →  BUY low / SELL high (assuming both resolve YES the
                            same way, which they do for FOMC questions)

The cross-venue arb is the more directly actionable signal — no modelling
risk, no fed-watch interpretation, just a price discrepancy between two venues
quoting the same fundamental question.

Outputs are grouped per bucket so a row contains both venues' prices side by
side, plus both edges. Sort key: max(|implied edge|, |arb spread|) — surfaces
whichever signal is bigger first.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ingestion import decision_calendar, fred_client, implied_path, kalshi_client, polymarket_client

# pp threshold — same on both edge types. Polymarket+Kalshi both have ~1 pp
# spread on liquid markets so 3 pp is conservative for either signal.
EDGE_THRESHOLD = 0.03


def _direction_implied(edge: float | None, venue: str) -> str:
    if edge is None:
        return "—"
    if edge > EDGE_THRESHOLD:
        return f"BUY YES ({venue})"
    if edge < -EDGE_THRESHOLD:
        return f"SELL YES ({venue})"
    return "—"


def _direction_arb(spread: float | None) -> str:
    """Cross-venue arbitrage direction.

    Convention: spread = polymarket - kalshi.
      spread > 0  → Polymarket is the dearer venue → SELL Polymarket / BUY Kalshi
      spread < 0  → Kalshi is the dearer venue     → BUY Polymarket / SELL Kalshi
    """
    if spread is None:
        return "—"
    if spread > EDGE_THRESHOLD:
        return "SELL POLY · BUY KALSHI"
    if spread < -EDGE_THRESHOLD:
        return "BUY POLY · SELL KALSHI"
    return "—"


def _current_fed_funds_rate() -> float | None:
    """Latest DFF reading from the cached FRED rates."""
    rates = fred_client.get_cached_rates()
    dff = next((s for s in rates["series"] if s["series_id"] == "DFF"), None)
    if dff and dff.get("latest"):
        return float(dff["latest"][1])
    return None


def compute() -> dict:
    """Build the edge view for the next FOMC. Always returns a dict; missing
    pieces are reported in ``errors`` rather than raised."""
    today = datetime.now(timezone.utc).date()
    cal = decision_calendar.upcoming(today, horizon_days=120)
    fomc = next((m for m in cal if m["cb"] == "US"), None)
    if not fomc:
        return {"as_of": today.isoformat(), "errors": ["no upcoming FOMC in horizon"]}

    meeting_date = date.fromisoformat(fomc["decision_date"])

    implied = implied_path.get_cached()
    probs = implied.get("probabilities", {}) or {}
    have_implied = bool(probs)

    poly_markets = polymarket_client.get_cached_for_meeting(meeting_date)
    current_rate = _current_fed_funds_rate()
    kalshi_markets = kalshi_client.get_cached_for_meeting(meeting_date, current_rate)

    # Index venue rows by bucket for O(1) join. If a venue has multiple markets
    # for the same bucket (rare), keep the most-liquid one — it's the price
    # the user can actually act on.
    by_bucket_poly: dict[str, dict] = {}
    for m in poly_markets:
        b = m["outcome_bucket"]
        if b not in by_bucket_poly or m["volume_24h"] > by_bucket_poly[b]["volume_24h"]:
            by_bucket_poly[b] = m

    by_bucket_kalshi: dict[str, dict] = {}
    for m in kalshi_markets:
        b = m["outcome_bucket"]
        if b not in by_bucket_kalshi or m["volume_24h"] > by_bucket_kalshi[b]["volume_24h"]:
            by_bucket_kalshi[b] = m

    all_buckets = set(by_bucket_poly) | set(by_bucket_kalshi) | set(probs)
    rows: list[dict] = []

    for bucket in all_buckets:
        poly = by_bucket_poly.get(bucket)
        kal = by_bucket_kalshi.get(bucket)
        if not poly and not kal:
            # Implied bucket with no market on either venue — skip; pure model
            # output without something to trade on isn't actionable here.
            continue

        poly_price = poly["polymarket_price"] if poly else None
        kal_price = kal["kalshi_price"] if kal else None
        implied_p = float(probs.get(bucket, 0.0)) if have_implied else None

        # Pick the dominant question text for display: prefer Polymarket
        # (longer, more descriptive) over Kalshi.
        question = (poly and poly["question"]) or (kal and kal["question"]) or bucket

        # Implied-vs-venue edges (per venue if both exist)
        edge_poly = (implied_p - poly_price) if (have_implied and poly_price is not None) else None
        edge_kal = (implied_p - kal_price) if (have_implied and kal_price is not None) else None

        # Cross-venue arb spread
        arb_spread = (poly_price - kal_price) if (poly_price is not None and kal_price is not None) else None

        rows.append({
            "outcome_bucket": bucket,
            "question": question,
            "polymarket_price": poly_price,
            "kalshi_price": kal_price,
            "implied_prob": round(implied_p, 4) if implied_p is not None else None,

            "edge_poly": round(edge_poly, 4) if edge_poly is not None else None,
            "edge_poly_pp": round(edge_poly * 100, 1) if edge_poly is not None else None,
            "edge_kalshi": round(edge_kal, 4) if edge_kal is not None else None,
            "edge_kalshi_pp": round(edge_kal * 100, 1) if edge_kal is not None else None,

            "arb_spread": round(arb_spread, 4) if arb_spread is not None else None,
            "arb_spread_pp": round(arb_spread * 100, 1) if arb_spread is not None else None,

            "direction_poly": _direction_implied(edge_poly, "Poly"),
            "direction_kalshi": _direction_implied(edge_kal, "Kalshi"),
            "direction_arb": _direction_arb(arb_spread),

            "polymarket_url": poly.get("url") if poly else None,
            "kalshi_url": kal.get("url") if kal else None,

            "polymarket_volume_24h": poly["volume_24h"] if poly else None,
            "kalshi_volume_24h": kal["volume_24h"] if kal else None,

            "kalshi_ticker": kal.get("ticker") if kal else None,
            "kalshi_event_ticker": kal.get("event_ticker") if kal else None,
        })

    # Sort by max(|implied edge|, |arb spread|) — biggest signal first.
    def _max_signal(r: dict) -> float:
        signals = [
            abs(r["edge_poly"]) if r["edge_poly"] is not None else 0.0,
            abs(r["edge_kalshi"]) if r["edge_kalshi"] is not None else 0.0,
            abs(r["arb_spread"]) if r["arb_spread"] is not None else 0.0,
        ]
        return max(signals) if signals else 0.0

    rows.sort(key=_max_signal, reverse=True)

    errors: list[str] = []
    if not probs:
        errors.append("implied probabilities unavailable (futures or rate data missing)")
    if not poly_markets:
        errors.append("no Polymarket markets matched (or API unreachable)")
    if not kalshi_markets:
        errors.append(
            "no Kalshi markets matched — KXFEDDECISION/KXFEDCOMBO contracts "
            "are listed but currently illiquid (Kalshi volume typically only "
            "picks up 1-2 weeks before each meeting)"
        )

    return {
        "as_of": today.isoformat(),
        "meeting": fomc,
        "current_rate": current_rate,
        "implied_probabilities": probs,
        "edge_threshold_pp": round(EDGE_THRESHOLD * 100, 1),
        "rows": rows,
        "errors": errors,
        # Phase-2 trading scope: Kalshi requires per-user RSA-signed orders; not
        # implemented in v0.5. The Trade-on-Kalshi link in the UI deep-links to
        # the user's own Kalshi account where they place the order themselves.
        "trading": {
            "polymarket": "deep-link only (no in-app trading)",
            "kalshi": "deep-link only (no in-app trading)",
            "phase_2_planned": "per-user API key + RSA-PSS order signing",
        },
    }


if __name__ == "__main__":
    import json
    import logging
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(compute(), indent=2))

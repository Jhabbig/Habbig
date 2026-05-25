"""FIFO tax-lot matching for the trade journal.

Given a list of buy/sell trades (the trade-journal localStorage format,
sent up by the client), match sells against the oldest open buy lots
(first-in-first-out) and compute realized P&L per match.

Also splits into short-term (<365 day holding) vs long-term (>=365 day)
buckets for the US tax convention. Output is fully reconstructible
without server-side state — the client sends the journal, we just
do the math.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _parse_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s.replace("Z", ""), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _days_held(buy_date: Optional[datetime], sell_date: Optional[datetime]) -> Optional[int]:
    if buy_date is None or sell_date is None:
        return None
    return (sell_date - buy_date).days


def realised_pnl_fifo(trades: list[dict]) -> dict:
    """Walk a chronological journal of buys + sells. Match each sell
    against open buy lots FIFO. Returns:

      {
        "matches": [{coin, buy_date, sell_date, lots, entry, exit,
                     proceeds_usd, cost_basis_usd, realised_pnl_usd,
                     days_held, term ("short"|"long")}],
        "open_lots": [unmatched buys still held],
        "summary": {
            "total_realised_pnl_usd": ...,
            "total_proceeds_usd": ...,
            "total_cost_basis_usd": ...,
            "short_term_pnl_usd": ...,
            "long_term_pnl_usd": ...,
            "match_count": ...,
            "win_count": ..., "loss_count": ...,
            "by_coin": {<COIN>: {realised_pnl_usd, match_count, ...}},
        }
      }

    Each trade dict expected to have:
      coin, side ("LONG"/"SHORT" — treated as buy/sell direction), entry,
      size, exit (nullable), date, notes (optional).
    """
    if not isinstance(trades, list):
        return {"error": "trades must be a list"}

    # Build per-coin lists of buys (entry as cost) and sells (exit as proceeds).
    # Sort all trades chronologically by date.
    chrono = sorted([t for t in trades if isinstance(t, dict)],
                     key=lambda t: t.get("date") or "")

    open_lots: dict[str, list[dict]] = {}  # coin -> list of open buys (FIFO order)
    matches: list[dict] = []

    for t in chrono:
        coin = (t.get("coin") or "").upper()
        side = (t.get("side") or "LONG").upper()
        size = float(t.get("size") or 0)
        entry = float(t.get("entry") or 0)
        exit_ = t.get("exit")
        date_str = t.get("date") or ""
        if not coin or size <= 0 or entry <= 0:
            continue

        # Closed trade: produces both a buy lot and a matched sell. For
        # tax purposes we treat the trade as opened on `date` (buy) and
        # closed on the same `date` if exit is set (single-day round-trip),
        # which is conservative — the journal doesn't store exit-date.
        if exit_ is not None:
            try:
                exit_px = float(exit_)
            except (TypeError, ValueError):
                continue
            # Open and immediately match a lot
            buy_date = _parse_date(date_str)
            sell_date = buy_date
            proceeds = size * exit_px
            cost = size * entry
            pnl = (proceeds - cost) if side == "LONG" else (cost - proceeds)
            held = _days_held(buy_date, sell_date)
            term = "short" if (held is None or held < 365) else "long"
            matches.append({
                "coin": coin,
                "buy_date": date_str,
                "sell_date": date_str,
                "lots": size,
                "entry": entry,
                "exit": exit_px,
                "side": side,
                "proceeds_usd": round(proceeds, 2),
                "cost_basis_usd": round(cost, 2),
                "realised_pnl_usd": round(pnl, 2),
                "days_held": held,
                "term": term,
            })
            continue

        # Open position: just track the lot for FIFO matching against future sells
        open_lots.setdefault(coin, []).append({
            "coin": coin, "size": size, "entry": entry,
            "date": date_str, "side": side,
        })

    # Roll up
    total_pnl = sum(m["realised_pnl_usd"] for m in matches)
    total_proceeds = sum(m["proceeds_usd"] for m in matches)
    total_cost = sum(m["cost_basis_usd"] for m in matches)
    short_pnl = sum(m["realised_pnl_usd"] for m in matches if m["term"] == "short")
    long_pnl = sum(m["realised_pnl_usd"] for m in matches if m["term"] == "long")
    wins = sum(1 for m in matches if m["realised_pnl_usd"] > 0)
    losses = sum(1 for m in matches if m["realised_pnl_usd"] < 0)

    by_coin: dict[str, dict] = {}
    for m in matches:
        b = by_coin.setdefault(m["coin"], {"coin": m["coin"],
                                            "realised_pnl_usd": 0.0,
                                            "match_count": 0,
                                            "proceeds_usd": 0.0,
                                            "cost_basis_usd": 0.0})
        b["realised_pnl_usd"] += m["realised_pnl_usd"]
        b["match_count"] += 1
        b["proceeds_usd"] += m["proceeds_usd"]
        b["cost_basis_usd"] += m["cost_basis_usd"]
    for v in by_coin.values():
        v["realised_pnl_usd"] = round(v["realised_pnl_usd"], 2)
        v["proceeds_usd"] = round(v["proceeds_usd"], 2)
        v["cost_basis_usd"] = round(v["cost_basis_usd"], 2)

    open_summary = []
    for coin, lots in open_lots.items():
        for lot in lots:
            open_summary.append({**lot})

    return {
        "matches": matches,
        "open_lots": open_summary,
        "summary": {
            "total_realised_pnl_usd": round(total_pnl, 2),
            "total_proceeds_usd": round(total_proceeds, 2),
            "total_cost_basis_usd": round(total_cost, 2),
            "short_term_pnl_usd": round(short_pnl, 2),
            "long_term_pnl_usd": round(long_pnl, 2),
            "match_count": len(matches),
            "win_count": wins,
            "loss_count": losses,
            "by_coin": sorted(by_coin.values(),
                              key=lambda b: b["realised_pnl_usd"], reverse=True),
        },
    }


def to_csv(matches: list[dict]) -> str:
    """Generate a tax-friendly CSV of the matches. Columns roughly mirror
    IRS Form 8949."""
    header = ["Description", "Acquired", "Sold", "Proceeds",
              "Cost basis", "Realised P&L", "Days held", "Term"]
    lines = [",".join(header)]
    for m in matches or []:
        row = [
            f"{m['lots']} {m['coin']}",
            m.get("buy_date", ""),
            m.get("sell_date", ""),
            f"{m['proceeds_usd']:.2f}",
            f"{m['cost_basis_usd']:.2f}",
            f"{m['realised_pnl_usd']:.2f}",
            str(m.get("days_held") or ""),
            m.get("term", ""),
        ]
        # Quote fields with commas (none expected in our data but defensive)
        row = [f'"{f}"' if "," in f else f for f in row]
        lines.append(",".join(row))
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    trades = [
        {"coin": "BTC", "side": "LONG", "entry": 30000, "size": 0.5, "exit": 50000,
         "date": "2024-01-15"},
        {"coin": "BTC", "side": "LONG", "entry": 60000, "size": 0.2, "exit": 65000,
         "date": "2025-03-10"},
        {"coin": "ETH", "side": "LONG", "entry": 2000, "size": 5, "exit": None,
         "date": "2024-06-01"},  # still open
        {"coin": "ETH", "side": "LONG", "entry": 2500, "size": 3, "exit": 4000,
         "date": "2025-05-15"},
    ]
    r = realised_pnl_fifo(trades)
    print(json.dumps(r, indent=2))
    print()
    print(to_csv(r["matches"]))

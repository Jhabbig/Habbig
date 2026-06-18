"""Pull real resolved Polymarket markets + trades, convert via polymarket_ingest,
write data/backtest/polymarket_real.json (SCHEMA.md shape). Run from gateway/:
    python3 scripts/build_polymarket_dataset.py --max-markets 800 --min-trades 30
"""
from __future__ import annotations
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations import polymarket_api as api
from integrations import polymarket_ingest as ing

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=800)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--out", default="data/backtest/polymarket_real.json")
    a = ap.parse_args()

    raw = api.fetch_resolved_markets(a.max_markets)
    print(f"pulled {len(raw)} closed markets")
    records, rejected = [], {"ambiguous": 0, "no_trades": 0, "no_forecasts": 0}
    for m in raw:
        if ing.decisive_outcome(m.get("outcomePrices"), m.get("outcomes")) is None:
            rejected["ambiguous"] += 1
            continue
        cond = m.get("conditionId")
        if not cond:
            continue
        trades = api.fetch_trades(cond, limit=1000)
        # integrity guard #1: keep only trades whose conditionId matches this market
        trades = [t for t in trades if str(t.get("conditionId")) == str(cond)]
        if len(trades) < a.min_trades:
            rejected["no_trades"] += 1
            continue
        rec = ing.build_market_record(m, trades)
        if rec is None:
            rejected["no_forecasts"] += 1
            continue
        records.append(rec)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"source": "polymarket", "synthetic": False, "markets": records},
              open(a.out, "w"), indent=2)
    print(f"wrote {len(records)} markets -> {a.out}")
    print(f"rejected: {rejected}")
    print(f"total forecasts: {sum(len(r['forecasts']) for r in records)}")

if __name__ == "__main__":
    main()

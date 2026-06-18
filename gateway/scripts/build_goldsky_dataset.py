"""Build the REAL backtest dataset from the Polymarket Goldsky subgraph.

Pulls per-wallet per-resolved-market realized P&L (marketProfit + nested
condition), groups by condition, and converts each resolved+decisive condition
into a SCHEMA.md market record via polymarket_ingest.build_record_from_profits.
Writes data/backtest/polymarket_real.json.

Run from gateway/:  python3 scripts/build_goldsky_dataset.py --max-rows 6000 --min-forecasts 5

v1 LIMITATION (see polymarket_ingest): forecasts are a coarse P&L-derived proxy
(0.75/0.25), price_timeline is a 0.5 placeholder, made_at = resolved-1day.
The real v1 signal is credibility separation across wallets on real resolved
markets — NOT narve-vs-market price. Refine with enrichedOrderFilled later.
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations import polymarket_api as api
from integrations import polymarket_ingest as ing

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=6000)
    ap.add_argument("--min-forecasts", type=int, default=5)
    ap.add_argument("--out", default="data/backtest/polymarket_real.json")
    a = ap.parse_args()

    rows = api.fetch_all_market_profits(max_rows=a.max_rows)
    print(f"pulled {len(rows)} marketProfit rows")

    # group rows by condition id; keep the condition object alongside
    by_cond = defaultdict(list)
    cond_obj = {}
    for r in rows:
        c = r.get("condition") or {}
        cid = c.get("id")
        if not cid:
            continue
        by_cond[cid].append(r)
        cond_obj[cid] = c
    print(f"distinct conditions: {len(by_cond)}")

    records, rej = [], {"not_decisive": 0, "too_few": 0}
    for cid, prows in by_cond.items():
        rec = ing.build_record_from_profits(cid, cond_obj[cid], prows,
                                            min_forecasts=a.min_forecasts)
        if rec is None:
            # distinguish the two reasons for the log
            if ing._condition_decisive(cond_obj[cid]) is None:
                rej["not_decisive"] += 1
            else:
                rej["too_few"] += 1
            continue
        records.append(rec)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"source": "polymarket-goldsky", "synthetic": False,
               "v1_limitation": "forecasts are a P&L-derived 0.75/0.25 proxy; "
               "price_timeline is a 0.5 placeholder; made_at=resolved-1day. "
               "Real signal: credibility separation across wallets on resolved markets.",
               "markets": records}, open(a.out, "w"), indent=2)
    print(f"wrote {len(records)} markets -> {a.out}")
    print(f"rejected: {rej}")
    print(f"total forecasts: {sum(len(r['forecasts']) for r in records)}")
    print(f"distinct wallets: {len({f['source_handle'] for r in records for f in r['forecasts']})}")

if __name__ == "__main__":
    main()

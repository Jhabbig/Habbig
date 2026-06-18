"""REAL credibility leaderboard from the Polymarket Goldsky subgraph.

The honest, sound product: rank wallets by REALIZED PROFIT across RESOLVED+
DECISIVE markets (marketProfit + condition). This needs NO directional
reconstruction — profit is profit, outcomes are on-chain. It answers "which
traders are consistently sharp" with real, unfakeable data.

NOT included (a separate, harder piece — see enrichedOrderFilled): per-market
YES/NO directional accuracy. marketProfit proves WHO is sharp, not which side
they predicted, so directional accuracy is deliberately out of scope here.

KNOWN LIMITATION (stated, not hidden): the subgraph returns marketProfit rows in
id order (wallets cluster by address prefix), so a bounded pull is a SAMPLE of
traders, not the global top. We report sample size explicitly; a full ranking
needs deep pagination or a server-side profit sort.

Run from gateway/:  python3 scripts/build_credibility_leaderboard.py --max-rows 12000 --min-markets 5
"""
from __future__ import annotations
import argparse, json, os, sys, datetime
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from integrations import polymarket_api as api
from integrations import polymarket_ingest as ing

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=12000)
    ap.add_argument("--min-markets", type=int, default=5)
    ap.add_argument("--out", default="data/backtest/credibility_leaderboard.json")
    ap.add_argument("--report", default="data/backtest/credibility_leaderboard.md")
    a = ap.parse_args()

    rows = api.fetch_all_market_profits(max_rows=a.max_rows)
    print(f"pulled {len(rows)} marketProfit rows")

    prof = defaultdict(float); n = defaultdict(int); wins = defaultdict(int)
    resolved_rows = 0
    for r in rows:
        c = r.get("condition") or {}
        if ing._condition_decisive(c) is None:
            continue
        u = (r.get("user") or {}).get("id")
        if not u:
            continue
        try:
            p = float(r.get("scaledProfit"))
        except (TypeError, ValueError):
            continue
        resolved_rows += 1
        prof[u] += p; n[u] += 1
        if p > 0:
            wins[u] += 1

    board = [
        {"wallet": u, "total_profit": round(prof[u], 2), "markets": n[u],
         "profitable_markets": wins[u], "hit_rate": round(wins[u] / n[u], 4)}
        for u in n if n[u] >= a.min_markets
    ]
    board.sort(key=lambda x: -x["total_profit"])

    meta = {
        "source": "polymarket-goldsky/marketProfit",
        "generated_at_note": "deterministic from on-chain realized P&L; NOT directional accuracy",
        "rows_pulled": len(rows),
        "resolved_decisive_profit_rows": resolved_rows,
        "wallets_ranked": len(board),
        "min_markets": a.min_markets,
        "limitation": "marketProfit rows return in id order -> this is a SAMPLE of traders, "
                      "not the global top. Sample size stated; full ranking needs deep pagination.",
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump({"meta": meta, "leaderboard": board}, open(a.out, "w"), indent=2)
    print(f"wrote {len(board)} ranked wallets -> {a.out}")

    # markdown report
    lines = [
        "# narve — Polymarket trader credibility leaderboard (REAL data)",
        "",
        "**Source:** Polymarket Goldsky subgraph `marketProfit` + `condition` — on-chain",
        "realized profit per wallet across **resolved, decisive** markets. No directional",
        "reconstruction, no proxy: profit and outcomes are on-chain facts.",
        "",
        f"**Sample:** {meta['rows_pulled']:,} P&L rows pulled, "
        f"{meta['resolved_decisive_profit_rows']:,} on resolved+decisive markets, "
        f"**{meta['wallets_ranked']} wallets** with ≥{a.min_markets} resolved markets.",
        "",
        "> **Limitation (stated):** the subgraph returns rows in id order, so this is a",
        "> SAMPLE of traders, not the global top. A full ranking needs deep pagination.",
        "> This proves WHO is sharp (realized profit) — NOT per-market YES/NO accuracy,",
        "> which needs the raw fills (enrichedOrderFilled) and is a separate next step.",
        "",
        "## Top 25 by realized profit",
        "",
        "| # | wallet | realized profit | resolved markets | profitable | hit-rate |",
        "| - | ------ | --------------- | ---------------- | ---------- | -------- |",
    ]
    for i, w in enumerate(board[:25], 1):
        lines.append(
            f"| {i} | `{w['wallet'][:16]}…` | ${w['total_profit']:,.0f} | "
            f"{w['markets']} | {w['profitable_markets']} | {w['hit_rate']*100:.0f}% |"
        )
    open(a.report, "w").write("\n".join(lines) + "\n")
    print(f"wrote report -> {a.report}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Wallet Co-Trading Cluster Detection (Sybil / Coordination Finder).

The hypothesis: a single insider often controls multiple wallets to
spread risk and disguise activity. Those wallets will bet on the same
outcomes within close time windows because they're being driven by the
same off-chain signal.

Algorithm:
  1. For every market, group all BUY trades by outcome.
  2. Within each (market, outcome) group, find wallets that bought
     within a short time window of each other (default: 60 minutes).
  3. Build an undirected graph where nodes = wallets, edges = "bought
     same thing within window". Edge weight = number of co-occurrences.
  4. Find connected components with ≥3 wallets.
  5. Score each cluster by: size, total volume, avg buy price (lower
     = more suspicious), and average pairwise time delta (closer = more
     suspicious — synchronized clicks vs. organic).

No external graph library — just plain Python sets and BFS.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# Default thresholds — tunable
COTRADE_WINDOW_SECONDS = 60 * 60       # 1 hour window for co-occurrence
MIN_TRADE_USD = 100                    # ignore dust trades
MIN_CLUSTER_SIZE = 3                   # smallest cluster to report
MIN_EDGE_WEIGHT = 2                    # need ≥2 co-occurrences before claiming an edge


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(v) -> int:
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def build_cotrade_edges(
    trades: list[dict],
    window_seconds: int = COTRADE_WINDOW_SECONDS,
    min_trade_usd: float = MIN_TRADE_USD,
) -> dict[tuple[str, str], dict[str, Any]]:
    """
    Walk through trades and emit weighted edges between wallets that bought
    the same outcome within `window_seconds` of each other.

    Returns a dict mapping (wallet_a, wallet_b) -> aggregated edge metadata.
    Wallet pairs are stored canonically (sorted alphabetically).
    """
    # Group trades by (market, outcome)
    by_market_outcome: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        side = (t.get("side") or "").upper()
        if side and side != "BUY":
            continue
        size = _safe_float(t.get("size"))
        price = _safe_float(t.get("price"))
        usd = size * price if price > 0 else size
        if usd < min_trade_usd:
            continue
        wallet = (t.get("proxyWallet") or t.get("maker_address") or "").lower()
        if not wallet:
            continue
        market_id = t.get("conditionId") or t.get("market") or t.get("slug") or ""
        outcome = t.get("outcome") or ""
        if not market_id or not outcome:
            continue
        ts = _safe_int(t.get("timestamp"))
        if ts <= 0:
            continue
        by_market_outcome[(market_id, outcome)].append({
            "wallet": wallet,
            "ts": ts,
            "usd": usd,
            "price": price,
            "title": t.get("title", ""),
            "name": t.get("name", ""),
            "pseudonym": t.get("pseudonym", ""),
            "outcome": outcome,
            "market_id": market_id,
        })

    edges: dict[tuple[str, str], dict[str, Any]] = {}

    for (market_id, outcome), group in by_market_outcome.items():
        if len(group) < 2:
            continue
        # Sort by timestamp so we can use a simple sliding-window scan
        group.sort(key=lambda x: x["ts"])
        n = len(group)
        for i in range(n):
            base = group[i]
            for j in range(i + 1, n):
                other = group[j]
                if other["ts"] - base["ts"] > window_seconds:
                    break
                if base["wallet"] == other["wallet"]:
                    continue
                a, b = sorted([base["wallet"], other["wallet"]])
                key = (a, b)
                if key not in edges:
                    edges[key] = {
                        "wallets": (a, b),
                        "weight": 0,
                        "co_markets": set(),
                        "total_usd": 0.0,
                        "min_delta_seconds": 10**9,
                        "avg_buy_price_sum": 0.0,
                        "names": {},
                        "co_trades": [],  # raw co-trade pairs for sample surfacing
                    }
                e = edges[key]
                e["weight"] += 1
                e["co_markets"].add(market_id)
                e["total_usd"] += base["usd"] + other["usd"]
                e["min_delta_seconds"] = min(e["min_delta_seconds"], abs(other["ts"] - base["ts"]))
                e["avg_buy_price_sum"] += (base["price"] + other["price"]) / 2
                for w, info in [(base["wallet"], base), (other["wallet"], other)]:
                    if w not in e["names"]:
                        e["names"][w] = info.get("pseudonym") or info.get("name") or w[:8]
                # Record up to 3 co-trade examples per edge to keep memory bounded
                if len(e["co_trades"]) < 3:
                    e["co_trades"].append({
                        "market_id": market_id,
                        "outcome": outcome,
                        "title": base.get("title") or other.get("title") or "",
                        "ts_a": base["ts"],
                        "ts_b": other["ts"],
                        "delta_seconds": abs(other["ts"] - base["ts"]),
                        "wallet_a": base["wallet"],
                        "wallet_b": other["wallet"],
                        "price_a": base["price"],
                        "price_b": other["price"],
                        "usd_a": round(base["usd"], 2),
                        "usd_b": round(other["usd"], 2),
                    })

    # Drop weak edges
    return {k: v for k, v in edges.items() if v["weight"] >= MIN_EDGE_WEIGHT}


def find_clusters(edges: dict[tuple[str, str], dict[str, Any]]) -> list[set[str]]:
    """Find connected components (clusters) via BFS."""
    adj: dict[str, set[str]] = defaultdict(set)
    for (a, b), _e in edges.items():
        adj[a].add(b)
        adj[b].add(a)

    visited: set[str] = set()
    clusters: list[set[str]] = []
    for node in adj:
        if node in visited:
            continue
        # BFS
        component: set[str] = set()
        stack = [node]
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            component.add(curr)
            for nbr in adj[curr]:
                if nbr not in visited:
                    stack.append(nbr)
        if len(component) >= MIN_CLUSTER_SIZE:
            clusters.append(component)
    return clusters


def score_cluster(
    wallets: set[str],
    edges: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Compute suspicion metrics for a cluster."""
    cluster_edges = [
        e for (a, b), e in edges.items()
        if a in wallets and b in wallets
    ]
    if not cluster_edges:
        return {}

    total_weight = sum(e["weight"] for e in cluster_edges)
    total_usd = sum(e["total_usd"] for e in cluster_edges)
    co_markets: set[str] = set()
    for e in cluster_edges:
        co_markets.update(e["co_markets"])

    edge_count = len(cluster_edges)
    avg_buy_price = (
        sum(e["avg_buy_price_sum"] for e in cluster_edges) / total_weight
        if total_weight else 0.0
    )
    min_delta = min(e["min_delta_seconds"] for e in cluster_edges)
    avg_delta = (
        sum(e["min_delta_seconds"] for e in cluster_edges) / edge_count
        if edge_count else 0
    )

    # Names lookup
    names: dict[str, str] = {}
    for e in cluster_edges:
        for w, n in e["names"].items():
            if w not in names:
                names[w] = n

    # Density: actual edges / possible edges (n choose 2)
    n = len(wallets)
    max_edges = n * (n - 1) / 2
    density = edge_count / max_edges if max_edges else 0.0

    # ─── Suspicion score ───
    score = 0
    reasons: list[str] = []

    if n >= 8:
        score += 35
        reasons.append(f"{n}-wallet cluster (large coordination ring)")
    elif n >= 5:
        score += 22
        reasons.append(f"{n}-wallet cluster")
    elif n >= 3:
        score += 12
        reasons.append(f"{n}-wallet cluster")

    if len(co_markets) >= 5:
        score += 25
        reasons.append(f"Active across {len(co_markets)} markets")
    elif len(co_markets) >= 3:
        score += 15
        reasons.append(f"Active across {len(co_markets)} markets")

    if density >= 0.75:
        score += 25
        reasons.append(f"Dense graph (density {density:.0%})")
    elif density >= 0.5:
        score += 15
        reasons.append(f"Densely connected (density {density:.0%})")

    if min_delta <= 60:
        score += 30
        reasons.append(f"Synchronized trades within {min_delta}s")
    elif min_delta <= 300:
        score += 18
        reasons.append(f"Trades within {min_delta // 60}min of each other")
    elif min_delta <= 900:
        score += 8
        reasons.append(f"Trades within {min_delta // 60}min")

    if avg_buy_price and avg_buy_price <= 0.15:
        score += 20
        reasons.append(f"Cluster favors long-shots (avg {avg_buy_price:.0%})")
    elif avg_buy_price and avg_buy_price <= 0.30:
        score += 8
        reasons.append(f"Cluster avg buy price {avg_buy_price:.0%}")

    if total_usd >= 100000:
        score += 20
        reasons.append(f"Cluster moved ${total_usd:,.0f}")
    elif total_usd >= 25000:
        score += 10
        reasons.append(f"Cluster moved ${total_usd:,.0f}")

    # Pick a sample of representative coordinated trades for the UI.
    # Prefer the tightest time-delta examples; cap at 6.
    all_co_trades: list[dict] = []
    seen_market_keys: set[tuple] = set()
    for e in cluster_edges:
        for ct in e.get("co_trades", []):
            all_co_trades.append(ct)
    all_co_trades.sort(key=lambda x: x["delta_seconds"])
    sample_trades: list[dict] = []
    for ct in all_co_trades:
        # Dedupe by (market_id, outcome) so we don't show 6 rows of the same market
        k = (ct["market_id"], ct["outcome"])
        if k in seen_market_keys:
            continue
        seen_market_keys.add(k)
        sample_trades.append(ct)
        if len(sample_trades) >= 6:
            break

    return {
        "wallet_count": n,
        "wallets": sorted(wallets),
        "names": names,
        "edge_count": edge_count,
        "total_co_trades": total_weight,
        "co_markets": len(co_markets),
        "co_market_ids": sorted(co_markets),
        "total_usd": round(total_usd, 2),
        "avg_buy_price": round(avg_buy_price, 4),
        "min_delta_seconds": min_delta,
        "avg_delta_seconds": int(avg_delta),
        "density": round(density, 3),
        "score": score,
        "reasons": reasons,
        "sample_trades": sample_trades,
    }


def detect_clusters(
    trades: list[dict],
    window_seconds: int = COTRADE_WINDOW_SECONDS,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
) -> dict[str, Any]:
    """
    Full pipeline: build edges → find components → score → return ranked.

    Returns a dict ready for serialization to the dashboard.
    """
    edges = build_cotrade_edges(trades, window_seconds=window_seconds)
    if not edges:
        return {
            "cluster_count": 0,
            "wallets_in_clusters": 0,
            "edges_total": 0,
            "clusters": [],
            "params": {
                "window_seconds": window_seconds,
                "min_cluster_size": min_cluster_size,
                "min_trade_usd": MIN_TRADE_USD,
            },
        }

    components = find_clusters(edges)
    components = [c for c in components if len(c) >= min_cluster_size]

    clusters_scored = []
    for comp in components:
        scored = score_cluster(comp, edges)
        if scored:
            clusters_scored.append(scored)
    clusters_scored.sort(key=lambda c: (c["score"], c["wallet_count"]), reverse=True)

    return {
        "cluster_count": len(clusters_scored),
        "wallets_in_clusters": sum(c["wallet_count"] for c in clusters_scored),
        "edges_total": len(edges),
        "clusters": clusters_scored[:30],
        "params": {
            "window_seconds": window_seconds,
            "min_cluster_size": min_cluster_size,
            "min_trade_usd": MIN_TRADE_USD,
        },
    }


def wallets_in_clusters_set(cluster_data: dict[str, Any]) -> set[str]:
    """Helper: flat set of all wallet addresses appearing in any cluster."""
    out: set[str] = set()
    for c in cluster_data.get("clusters", []):
        for w in c.get("wallets", []):
            out.add(w.lower())
    return out


if __name__ == "__main__":
    # Smoke test with synthetic coordinated trades
    import time
    base_ts = int(time.time())
    fake = []
    coord_wallets = [f"0x{i:040x}" for i in range(5)]
    for w in coord_wallets:
        fake.append({
            "proxyWallet": w,
            "conditionId": "market-A",
            "outcome": "Yes",
            "side": "BUY",
            "size": 5000,
            "price": 0.08,
            "timestamp": base_ts + 100,  # all within 100s
        })
        fake.append({
            "proxyWallet": w,
            "conditionId": "market-B",
            "outcome": "No",
            "side": "BUY",
            "size": 4000,
            "price": 0.12,
            "timestamp": base_ts + 200,  # also within window
        })
    # Add some noise wallets
    for i in range(20):
        fake.append({
            "proxyWallet": f"0xnoise{i:036x}",
            "conditionId": f"market-rand-{i % 4}",
            "outcome": "Yes",
            "side": "BUY",
            "size": 300,
            "price": 0.5,
            "timestamp": base_ts + i * 7200,
        })

    result = detect_clusters(fake)
    print(f"Found {result['cluster_count']} clusters")
    for c in result["clusters"]:
        print(f"  [{c['score']}] {c['wallet_count']} wallets, {c['co_markets']} markets, "
              f"density={c['density']}, min_delta={c['min_delta_seconds']}s")
        for r in c["reasons"]:
            print(f"    - {r}")

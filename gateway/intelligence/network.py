"""Source network analysis — echo chamber detection + independence scoring.

Entry points:

    compute_all_relationships()  — full pairwise computation (weekly job)
    compute_network_adjusted_consensus(predictions, relationships)
                                 — enhanced betyc probability
    compute_independent_signal_score(agreement, accuracy, n)
                                 — single-pair score
    classify_relationship(agreement, accuracy)
                                 — echo_chamber / independent / complementary / opposing
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import db


log = logging.getLogger("network")


# ── Single-pair scoring ───────────────────────────────────────────────────


def compute_independent_signal_score(
    agreement_rate: float,
    both_correct_rate: float,
    markets_both_predicted: int,
) -> float:
    """Measures how independent two sources' signals are.

    High agreement + low accuracy = echo chamber (low score)
    High agreement + high accuracy = complementary (medium-high score)
    Low agreement = independent signals (high score)

    Returns a float in [0.0, 1.0].
    """
    base_independence = 1.0 - agreement_rate
    # Accurate agreement is less bad than inaccurate agreement:
    # two sources who always agree AND are always right are genuinely
    # complementary, not an echo chamber.
    accuracy_adjustment = both_correct_rate * 0.3
    raw_score = base_independence + accuracy_adjustment
    # Confidence penalty for small samples — scale linearly from 0 at 0
    # shared markets to 1.0 at 10+ shared markets.
    confidence = min(markets_both_predicted / 10.0, 1.0)
    return max(0.0, min(1.0, raw_score * confidence))


def classify_relationship(agreement_rate: float, both_correct_rate: float) -> str:
    """Classify a source pair into one of four buckets.

    echo_chamber:   agreement > 85% AND joint accuracy < 65%
    complementary:  agreement > 70% AND joint accuracy > 75%
    opposing:       agreement < 30%
    independent:    everything else (0.3 <= agreement <= 0.85)
    """
    if agreement_rate > 0.85 and both_correct_rate < 0.65:
        return "echo_chamber"
    if agreement_rate > 0.70 and both_correct_rate > 0.75:
        return "complementary"
    if agreement_rate < 0.30:
        return "opposing"
    return "independent"


# ── Full network computation (weekly job) ─────────────────────────────────


MIN_SHARED_MARKETS = 5


def compute_all_relationships() -> dict[str, Any]:
    """Compute pairwise relationships for every source pair with >= 5 shared markets.

    Algorithm:
      1. For each pair of sources, find markets where both made predictions
      2. Compute agreement_rate (same direction on the same market)
      3. Compute both_correct_rate (both correct on resolved markets)
      4. Score + classify
      5. Detect echo chamber clusters via union-find on agreement > 0.85
      6. Store the full network snapshot

    Returns summary stats.
    """
    log.info("Starting source network computation")
    start = time.monotonic()

    # Step 1: get all sources with resolved predictions
    with db.conn() as c:
        sources = [
            r["source_handle"] for r in c.execute(
                "SELECT DISTINCT source_handle FROM predictions "
                "WHERE resolved = 1 AND resolved_correct IS NOT NULL"
            ).fetchall()
        ]

    if len(sources) < 2:
        log.info("Fewer than 2 sources with resolved predictions; skipping")
        return {"computed": 0, "sources": len(sources)}

    # Step 2: for each pair, compute metrics
    # Build a market_id -> {handle: direction, correct} lookup first for efficiency
    with db.conn() as c:
        all_preds = c.execute(
            "SELECT source_handle, market_id, direction, resolved, resolved_correct "
            "FROM predictions "
            "WHERE market_id IS NOT NULL AND market_id != ''"
        ).fetchall()

    # market_id -> {handle: {direction, resolved_correct}}
    market_sources: dict[str, dict[str, dict]] = {}
    for p in all_preds:
        mid = p["market_id"]
        if mid not in market_sources:
            market_sources[mid] = {}
        market_sources[mid][p["source_handle"]] = {
            "direction": (p["direction"] or "").upper(),
            "resolved": bool(p["resolved"]),
            "correct": p["resolved_correct"],
        }

    # For each pair, accumulate shared-market stats
    pair_stats: dict[tuple[str, str], dict] = {}

    for mid, src_map in market_sources.items():
        handles = sorted(src_map.keys())
        for i, a in enumerate(handles):
            for b in handles[i + 1:]:
                key = (a, b)  # already sorted
                if key not in pair_stats:
                    pair_stats[key] = {
                        "shared": 0, "agreed": 0,
                        "both_resolved": 0, "both_correct": 0,
                    }
                ps = pair_stats[key]
                ps["shared"] += 1

                da = src_map[a]["direction"]
                db_dir = src_map[b]["direction"]
                if da and db_dir and da == db_dir:
                    ps["agreed"] += 1

                if src_map[a]["resolved"] and src_map[b]["resolved"]:
                    if src_map[a]["correct"] is not None and src_map[b]["correct"] is not None:
                        ps["both_resolved"] += 1
                        if src_map[a]["correct"] and src_map[b]["correct"]:
                            ps["both_correct"] += 1

    # Step 3: compute + upsert relationships for pairs with enough data
    computed = 0
    all_echo = []  # [(a, b)] for echo chamber edges

    for (a, b), ps in pair_stats.items():
        if ps["shared"] < MIN_SHARED_MARKETS:
            continue

        agreement = ps["agreed"] / ps["shared"] if ps["shared"] > 0 else 0.0
        both_correct = (
            ps["both_correct"] / ps["both_resolved"]
            if ps["both_resolved"] > 0
            else 0.0
        )
        score = compute_independent_signal_score(agreement, both_correct, ps["shared"])
        rel_type = classify_relationship(agreement, both_correct)

        db.upsert_source_relationship(
            source_a=a,
            source_b=b,
            markets_both_predicted=ps["shared"],
            agreement_rate=agreement,
            both_correct_rate=both_correct,
            independent_signal_score=score,
            relationship_type=rel_type,
        )
        computed += 1

        if rel_type == "echo_chamber":
            all_echo.append((a, b))

    # Step 4: detect echo chamber clusters via union-find
    clusters = _find_clusters(all_echo)

    # Step 5: find most independent sources
    independent = _find_most_independent(sources)

    # Step 6: save network snapshot
    db.save_source_network(
        total_sources=len(sources),
        total_relationships=computed,
        echo_chamber_clusters=clusters,
        most_independent_sources=independent,
    )

    duration = round(time.monotonic() - start, 2)
    log.info(
        "Network computation complete: %d relationships, %d echo clusters, %.1fs",
        computed, len(clusters), duration,
    )
    return {
        "computed": computed,
        "sources": len(sources),
        "echo_clusters": len(clusters),
        "duration": duration,
    }


def _find_clusters(edges: list[tuple[str, str]]) -> list[list[str]]:
    """Union-find to group echo-chamber edges into connected components."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    # Group by root
    groups: dict[str, list[str]] = {}
    for node in parent:
        root = find(node)
        groups.setdefault(root, []).append(node)

    # Only return clusters of size >= 2
    return [sorted(members) for members in groups.values() if len(members) >= 2]


def _find_most_independent(sources: list[str], top_n: int = 10) -> list[dict]:
    """Sources whose average independence score across all relationships is highest."""
    scores: dict[str, list[float]] = {s: [] for s in sources}
    rels = db.list_all_source_relationships(limit=5000)
    for r in rels:
        a, b = r["source_a"], r["source_b"]
        sc = r["independent_signal_score"]
        if a in scores:
            scores[a].append(sc)
        if b in scores:
            scores[b].append(sc)

    ranked = []
    for handle, scs in scores.items():
        if not scs:
            continue
        avg = sum(scs) / len(scs)
        cred_row = db.get_source_credibility(handle)
        cred = cred_row["global_credibility"] if cred_row else 0.5
        ranked.append({
            "handle": handle,
            "avg_independence": round(avg, 3),
            "credibility": round(cred, 3),
            "relationship_count": len(scs),
        })
    ranked.sort(key=lambda x: x["avg_independence"], reverse=True)
    return ranked[:top_n]


# ── Network-adjusted consensus ────────────────────────────────────────────


def compute_network_adjusted_consensus(
    predictions: list,
    relationships: Optional[dict] = None,
) -> dict[str, Any]:
    """Enhanced betyc probability that accounts for echo chambers.

    Sources in the same echo chamber are down-weighted: within each cluster,
    only the highest-credibility source keeps full weight; others get
    weight *= independence_score. Across clusters, all sources keep full weight.

    Falls back to the standard `calculate_betyc_probability` if no
    relationship data is available.

    Returns the same dict shape as calculate_betyc_probability plus:
      - effective_signal_count: int (after de-duplication)
      - naive_yes_probability: float (before network adjustment)
      - network_adjusted: bool
      - echo_chambers_found: list of clusters active on this market
    """
    if not predictions:
        base = db.calculate_betyc_probability([])
        base.update({
            "effective_signal_count": 0,
            "naive_yes_probability": None,
            "network_adjusted": False,
            "echo_chambers_found": [],
        })
        return base

    # Get naive probability first
    naive = db.calculate_betyc_probability(predictions)
    naive_yes = naive.get("betyc_yes_probability")

    if not relationships:
        naive.update({
            "effective_signal_count": naive.get("betyc_source_count", 0),
            "naive_yes_probability": naive_yes,
            "network_adjusted": False,
            "echo_chambers_found": [],
        })
        return naive

    # Build handle -> prediction lookup
    handle_pred: dict[str, dict] = {}
    for p in predictions:
        h = p.get("source_handle")
        if h:
            handle_pred[h] = p

    handles = list(handle_pred.keys())
    if len(handles) < 2:
        naive.update({
            "effective_signal_count": len(handles),
            "naive_yes_probability": naive_yes,
            "network_adjusted": False,
            "echo_chambers_found": [],
        })
        return naive

    # Build echo-chamber clusters among the handles predicting on THIS market
    echo_edges = []
    for (a, b), rel in relationships.items():
        if a in handle_pred and b in handle_pred:
            if rel["relationship_type"] == "echo_chamber":
                echo_edges.append((a, b))
    clusters = _find_clusters(echo_edges)

    # Assign weights: within each cluster, highest-credibility source gets 1.0,
    # others get their independence_score (0.0–1.0).
    clustered_handles: set[str] = set()
    weight: dict[str, float] = {h: 1.0 for h in handles}

    for cluster in clusters:
        members = [h for h in cluster if h in handle_pred]
        if len(members) < 2:
            continue
        clustered_handles.update(members)
        # Sort by credibility descending
        members.sort(
            key=lambda h: (
                handle_pred[h].get("category_credibility")
                or handle_pred[h].get("global_credibility")
                or 0.5
            ),
            reverse=True,
        )
        # Leader keeps weight 1.0; followers get independence_score
        for follower in members[1:]:
            # Find the relationship between leader and follower
            pair = tuple(sorted([members[0], follower]))
            rel = relationships.get(pair)
            if rel:
                weight[follower] = rel["independent_signal_score"]
            else:
                weight[follower] = 0.3  # default penalty if no direct edge

    # Compute network-adjusted probability
    weighted_sum = 0.0
    weight_total = 0.0

    for h, p in handle_pred.items():
        cred = p.get("category_credibility") or p.get("global_credibility") or 0.5
        prob = p.get("predicted_probability")
        if prob is not None:
            w = cred * weight[h]
            weighted_sum += prob * w
            weight_total += w
        else:
            direction = (p.get("direction") or "").upper()
            if direction == "YES":
                inferred = 0.5 + (cred - 0.5) * 0.8
            elif direction == "NO":
                inferred = 0.5 - (cred - 0.5) * 0.8
            else:
                continue
            w = cred * weight[h]
            weighted_sum += inferred * w
            weight_total += w

    if weight_total == 0:
        naive.update({
            "effective_signal_count": len(handles),
            "naive_yes_probability": naive_yes,
            "network_adjusted": False,
            "echo_chambers_found": [],
        })
        return naive

    adjusted_prob = max(0.05, min(0.95, weighted_sum / weight_total))
    effective_count = sum(1 for h in handles if weight[h] > 0.5)

    return {
        "betyc_yes_probability": round(adjusted_prob, 4),
        "betyc_no_probability": round(1 - adjusted_prob, 4),
        "betyc_edge": naive.get("betyc_edge"),
        "betyc_source_count": len(handles),
        "betyc_confidence": naive.get("betyc_confidence"),
        "effective_signal_count": effective_count,
        "naive_yes_probability": naive_yes,
        "network_adjusted": True,
        "echo_chambers_found": [
            c for c in clusters if any(h in handle_pred for h in c)
        ],
    }

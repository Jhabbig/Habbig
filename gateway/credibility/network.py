"""Source network analysis.

Computes relationships between pairs of sources based on their shared
prediction history:

  echo_chamber  — agreement > 0.85 AND both_correct_rate < 0.65
                  (they always agree, but that agreement doesn't produce
                   better-than-average outcomes → redundant noise)
  complementary — agreement > 0.7 AND both_correct_rate > 0.75
                  (they agree often AND that agreement is predictive)
  independent   — 0.40 ≤ agreement ≤ 0.65
                  (they disagree just often enough to be checking each
                   other, which is what you want)
  opposing      — agreement < 0.30
  neutral       — anything else; not stored (the UI only renders strong
                  classifications to avoid noise)

Minimum sample: 5 shared markets — below that, pair stats aren't worth
classifying.

Network-adjusted consensus: cluster sources by echo chamber, then when
aggregating predictions down-weight multiple voices from the same
cluster so 10 echo-chamber members of one cluster don't outvote 3
genuinely-independent sources.

All functions are pure. The DB loader / writer lives in the job
wrapper (jobs/compute_source_relationships.py).
"""

from __future__ import annotations

from typing import Any


MIN_SHARED_MARKETS = 5


def pairwise_stats(
    a_records: list[dict],
    b_records: list[dict],
) -> dict | None:
    """Compute the shared-market stats between two sources.

    Each record must have ``market_slug``, ``direction`` ("YES"/"NO"),
    and ``resolved_correct`` (0/1/None). Records without a resolution
    don't count toward correctness rates but do count toward agreement.

    Returns None when fewer than :data:`MIN_SHARED_MARKETS` markets
    appear in both sources' histories.
    """
    a_by_market = {r["market_slug"]: r for r in (a_records or []) if r.get("market_slug")}
    b_by_market = {r["market_slug"]: r for r in (b_records or []) if r.get("market_slug")}
    shared_slugs = set(a_by_market) & set(b_by_market)
    if len(shared_slugs) < MIN_SHARED_MARKETS:
        return None

    agree_count = 0
    resolved_both_count = 0
    both_correct_count = 0
    for slug in shared_slugs:
        ra = a_by_market[slug]
        rb = b_by_market[slug]
        dir_a = str(ra.get("direction") or "").upper()
        dir_b = str(rb.get("direction") or "").upper()
        if dir_a and dir_b and dir_a == dir_b:
            agree_count += 1
            # "Both correct" only meaningful when they agreed AND both resolved.
            if ra.get("resolved_correct") is not None and rb.get("resolved_correct") is not None:
                resolved_both_count += 1
                if ra.get("resolved_correct") and rb.get("resolved_correct"):
                    both_correct_count += 1

    shared = len(shared_slugs)
    agreement_rate = agree_count / shared
    both_correct_rate = (
        both_correct_count / resolved_both_count
        if resolved_both_count else 0.0
    )

    # Independence score: inverse of agreement plus outcome bonus when
    # they agree AND that agreement is correct. Range roughly [0, 1.3],
    # clamped for storage. Higher = more useful to keep both voices.
    independent = (1.0 - agreement_rate) + (both_correct_rate * 0.3)
    independent = max(0.0, min(1.0, independent))

    return {
        "shared_markets": shared,
        "agreement_rate": round(agreement_rate, 6),
        "both_correct_rate": round(both_correct_rate, 6),
        "independent_signal_score": round(independent, 6),
    }


def classify_relationship(stats: dict) -> str:
    """Map stats → relationship_type."""
    a = stats.get("agreement_rate") or 0.0
    bcr = stats.get("both_correct_rate") or 0.0
    if a > 0.85 and bcr < 0.65:
        return "echo_chamber"
    if a > 0.70 and bcr > 0.75:
        return "complementary"
    if 0.40 <= a <= 0.65:
        return "independent"
    if a < 0.30:
        return "opposing"
    return "neutral"


def echo_chamber_clusters(relationships: list[dict]) -> list[list[str]]:
    """Union-find over ``echo_chamber`` edges.

    Returns a list of clusters, each cluster is a list of source handles.
    Singleton nodes (no echo-chamber edges) are excluded — they aren't
    "clusters" in any useful sense.

    Each input dict must carry ``source_a``, ``source_b``, and
    ``relationship_type``.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        parent[find(x)] = find(y)

    for rel in relationships or []:
        if rel.get("relationship_type") != "echo_chamber":
            continue
        a = rel.get("source_a")
        b = rel.get("source_b")
        if not a or not b:
            continue
        union(a, b)

    groups: dict[str, list[str]] = {}
    for node in parent:
        root = find(node)
        groups.setdefault(root, []).append(node)

    return [sorted(members) for members in groups.values() if len(members) > 1]


def network_adjusted_consensus(
    predictions: list[dict],
    clusters: list[list[str]],
    *,
    cluster_cap_weight: float = 1.0,
) -> dict:
    """Aggregate a list of (source_handle, direction, credibility) predictions
    into a consensus probability, down-weighting echo-chamber duplicates.

    ``predictions`` shape: ``[{"source_handle", "direction", "credibility",
    "predicted_probability"}, ...]``.

    Each cluster contributes at most ``cluster_cap_weight`` total credibility
    weight, no matter how many of its members voice a prediction on the
    market. Sources not in any cluster contribute their full credibility.

    Returns::

        {
          "yes_weight":       float,
          "no_weight":        float,
          "consensus_yes":    float (0–1),
          "effective_signal_count": int   # unique voices post-adjustment
        }
    """
    cluster_of: dict[str, int] = {}
    for idx, members in enumerate(clusters or []):
        for m in members:
            cluster_of[m] = idx

    # Per-cluster accumulator: weight + flag
    cluster_totals: dict[int, dict[str, float]] = {}
    ungrouped_yes = 0.0
    ungrouped_no = 0.0
    ungrouped_count = 0

    for pred in predictions or []:
        handle = pred.get("source_handle")
        direction = str(pred.get("direction") or "").upper()
        cred = float(pred.get("credibility") or 0.5)
        if direction not in ("YES", "NO"):
            continue
        cluster = cluster_of.get(handle)
        if cluster is None:
            ungrouped_count += 1
            if direction == "YES":
                ungrouped_yes += cred
            else:
                ungrouped_no += cred
            continue
        bucket = cluster_totals.setdefault(cluster, {"yes": 0.0, "no": 0.0, "n": 0})
        bucket["n"] += 1
        if direction == "YES":
            bucket["yes"] += cred
        else:
            bucket["no"] += cred

    yes_weight = ungrouped_yes
    no_weight = ungrouped_no
    effective = ungrouped_count

    for bucket in cluster_totals.values():
        total = bucket["yes"] + bucket["no"]
        if total <= 0:
            continue
        # Cap total weight at cluster_cap_weight, distributed proportionally.
        scale = min(1.0, cluster_cap_weight / max(total, 1e-9))
        yes_weight += bucket["yes"] * scale
        no_weight += bucket["no"] * scale
        effective += 1  # Cluster counts as a single effective voice.

    total = yes_weight + no_weight
    consensus_yes = (yes_weight / total) if total > 0 else 0.5

    return {
        "yes_weight": round(yes_weight, 6),
        "no_weight": round(no_weight, 6),
        "consensus_yes": round(consensus_yes, 6),
        "effective_signal_count": effective,
    }

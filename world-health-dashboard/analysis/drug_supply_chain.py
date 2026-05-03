"""Drug supply chain — per-drug profile + weak-point score.

Combines openFDA labels (manufacturer count), FDA Drug Shortages (current +
historical), and FDA recalls (5-yr lookback) into a single drug record. The
weak-point score is a transparent 0-10 heuristic — *not* a real-time supply-
chain monitor — that flags drugs likely to bottleneck under stress:

  +3 single source (1 manufacturer in FDA label data)
  +2 limited supply (2-4 manufacturers)
  +0 resilient (5+ manufacturers)

  +3 currently in active FDA shortage
  +1 per past shortage event (cap at +2)

  +2 ≥1 Class I recall in last 5 years
  +1 ≥3 recalls (any class) in last 5 years

  +1 fewer than 5 FDA labels total (proxy for niche / generic-shy market)

Score interpretation (rough):
  0-2  resilient
  3-5  watch list
  6-7  fragile
  8+   actively at risk

The heuristic is intentionally conservative — these are signals, not
predictions. Vincristine famously scores high (single source, low volume,
chemotherapy critical), and our score should reflect that.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ingestion import fda_recalls, fda_shortages, openfda_drugs, rxnorm

log = logging.getLogger(__name__)


def _shortage_summary(shortage_entries: list[dict]) -> dict:
    """Reduce shortage entries to a compact summary."""
    current = [e for e in shortage_entries
               if (e.get("status") or "").lower() == "current"]
    resolved = [e for e in shortage_entries
                if (e.get("status") or "").lower() == "resolved"]
    reasons = [(e.get("shortage_reason") or "Unspecified") for e in current if e.get("shortage_reason")]
    companies = sorted({(e.get("company_name") or "Unknown") for e in current})

    return {
        "active_shortage":   len(current) > 0,
        "current_count":     len(current),
        "resolved_count":    len(resolved),
        "current_companies": companies[:10],
        "current_reasons":   list(set(reasons))[:5],
        "current_entries": [
            {
                "generic":  e.get("generic_name"),
                "brand":    e.get("proprietary_name"),
                "company":  e.get("company_name"),
                "form":     e.get("dosage_form"),
                "status":   e.get("status"),
                "reason":   e.get("shortage_reason"),
                "updated":  e.get("update_date"),
                "posted":   e.get("initial_posting_date"),
            }
            for e in current[:8]
        ],
    }


def _weak_point_score(labels: dict, shortage: dict, recalls: dict) -> dict:
    score = 0
    flags: list[str] = []

    n = labels.get("manufacturer_count", 0)
    if n == 1:
        score += 3
        flags.append(f"single source — only 1 manufacturer ({(labels.get('manufacturers') or ['?'])[0]})")
    elif 2 <= n <= 4:
        score += 2
        flags.append(f"limited supply — {n} manufacturers")

    if shortage.get("active_shortage"):
        score += 3
        flags.append(f"currently in FDA shortage ({shortage['current_count']} entries)")
    past = min(shortage.get("resolved_count", 0), 2)
    if past:
        score += past
        flags.append(f"{shortage['resolved_count']} past shortage event(s)")

    class_i = (recalls.get("by_classification") or {}).get("Class I", 0)
    if class_i >= 1:
        score += 2
        flags.append(f"{class_i} Class-I recall(s) in last 5 yrs")
    if (recalls.get("recent_count") or 0) >= 3:
        score += 1
        flags.append(f"{recalls['recent_count']} recalls in last 5 yrs")

    if labels.get("total_labels", 0) < 5:
        score += 1
        flags.append("thin FDA label set — niche or specialist drug")

    score = min(score, 10)
    if score >= 8:
        rating = "actively at risk"
    elif score >= 6:
        rating = "fragile"
    elif score >= 3:
        rating = "watch list"
    else:
        rating = "resilient"
    return {"score": score, "rating": rating, "flags": flags}


def profile(generic: str) -> dict:
    """Full per-drug supply-chain profile + weak-point score."""

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_labels   = pool.submit(openfda_drugs.lookup, generic)
        f_short    = pool.submit(fda_shortages.for_drug, generic)
        f_recalls  = pool.submit(fda_recalls.lookup, generic)
        f_brands   = pool.submit(rxnorm.resolve, generic)
        labels    = f_labels.result()
        shortages = f_short.result()
        recalls   = f_recalls.result()
        brands    = f_brands.result()

    short_summary = _shortage_summary(shortages)
    score = _weak_point_score(labels, short_summary, recalls)

    return {
        "generic":      generic,
        "rxnorm":       brands,
        "labels":       {
            "total_labels":       labels.get("total_labels", 0),
            "manufacturers":      labels.get("manufacturers", []),
            "manufacturer_count": labels.get("manufacturer_count", 0),
            "brands":             labels.get("brands", []),
            "rxcuis":             labels.get("rxcuis", []),
            "substances":         labels.get("substances", []),
            "routes":             labels.get("routes", []),
            "pharm_class":        labels.get("pharm_class", []),
        },
        "shortage":     short_summary,
        "recalls": {
            "total_alltime":      recalls.get("total_recalls_alltime"),
            "recent_count":       recalls.get("recent_count"),
            "by_classification":  recalls.get("by_classification"),
            "by_firm":            recalls.get("by_firm"),
            "recent":             recalls.get("recent_recalls", []),
            "lookback_years":     recalls.get("lookback_years"),
        },
        "weak_point":   score,
    }


def shortage_overview() -> dict:
    """National picture: count by status + top therapeutic categories."""
    payload = fda_shortages.fetch()
    cur = [r for r in payload.get("all_entries", [])
           if (r.get("status") or "").lower() == "current"]
    def _flatten(v) -> str:
        """openFDA returns therapeutic_category as either a string or list."""
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v if x)
        return (v or "Unspecified").strip() or "Unspecified"

    cats: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in cur:
        cat = _flatten(r.get("therapeutic_category"))
        cats[cat] = cats.get(cat, 0) + 1
        reason = _flatten(r.get("shortage_reason"))
        reasons[reason] = reasons.get(reason, 0) + 1

    top_cats = sorted(cats.items(), key=lambda kv: -kv[1])[:15]
    top_reasons = sorted(reasons.items(), key=lambda kv: -kv[1])[:10]

    # Latest 30 most-recently-updated currently-in-shortage entries
    cur_sorted = sorted(cur, key=lambda r: (r.get("update_date") or ""), reverse=True)
    latest = [
        {
            "generic":  r.get("generic_name"),
            "brand":    r.get("proprietary_name"),
            "company":  r.get("company_name"),
            "category": r.get("therapeutic_category"),
            "form":     r.get("dosage_form"),
            "status":   r.get("status"),
            "reason":   r.get("shortage_reason"),
            "updated":  r.get("update_date"),
        }
        for r in cur_sorted[:30]
    ]

    return {
        "total_active":     len(cur),
        "total_alltime":    payload.get("total_entries"),
        "by_category":      dict(top_cats),
        "by_reason":        dict(top_reasons),
        "latest_updates":   latest,
        "fetched_at":       payload.get("fetched_at"),
    }

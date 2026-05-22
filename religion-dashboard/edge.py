"""Polymarket edge calculator for religion-tagged markets.

For each live market, attempt to match it to one of our quantitative
models:

  - Leader-actuarial: "Will [Leader] still be alive on [date]?"
    → P(yes) = survival_prob(age, sex, months_until_end, hr=0.85)
  - Leader-mortality: "Will [Leader] die by [date]?"
    → P(yes) = 1 - survival_prob(...)
  - Papabile: "Will [Cardinal] be the next Pope?"
    → P(yes) = papabile_prior / 100  (capped at the prior)
  - Vacancy: "Will the Holy See be vacant before [date]?"
    → P(yes) = 1 - survival_prob(Francis's age, M, months, hr=0.85)

Edge = model_p − implied_p (in percentage points). Positive edge means
the market under-prices YES (buy YES is profitable in expectation);
negative edge means buy NO. Markets we can't match return None for the
model fields and are excluded from the ranking.

CAVEATS:
  - The actuarial prior is SSA + a 0.85 hazard ratio. It's a baseline,
    not the truth — domain news (e.g., Pope hospitalised) should adjust.
  - The papabile prior is journalistic consensus, not a fundamental.
    Once a conclave starts, market liquidity and our model both move
    on new information faster than this static table.
  - Many Polymarket questions are ambiguously worded ("Pope dies in
    2025?" vs "death of current Pope?"). Conservative matchers only —
    when in doubt, return None.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import actuarial
import historical_leaders


# ─── Date parsing for market end-dates ──────────────────────────────────────

def _parse_iso_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Last resort: leading 10 chars
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _months_between(d1: date, d2: date) -> float:
    """Months between two dates as a float."""
    if d2 <= d1:
        return 0.0
    return (d2 - d1).days / 30.4375


# ─── Keyword classification ─────────────────────────────────────────────────

_DEATH_WORDS  = ("die", "died", "dies", "dying", "death", "pass away", "passing")
_ALIVE_WORDS  = ("alive", "still alive", "survive", "survives", "live to")
_VACANCY_WORDS= ("sede vacante", "holy see vacant", "vacancy", "vacate", "vacant")
_NEXT_POPE    = ("next pope", "next pontiff", "successor of francis", "successor pope",
                 "new pope", "elected pope", "to be pope", "be pope")


def _classify_intent(q: str) -> Optional[str]:
    """Returns one of: 'mortality', 'survival', 'vacancy', 'succession', or None."""
    ql = q.lower()
    if any(w in ql for w in _VACANCY_WORDS):
        return "vacancy"
    if any(w in ql for w in _NEXT_POPE):
        return "succession"
    has_death = any(w in ql for w in _DEATH_WORDS)
    has_alive = any(w in ql for w in _ALIVE_WORDS)
    if has_death and not has_alive:
        return "mortality"
    if has_alive and not has_death:
        return "survival"
    return None


# ─── Name matching ──────────────────────────────────────────────────────────

def _find_leader(q: str, leaders: list[dict]) -> Optional[dict]:
    """Match a market question to one of our tracked leaders by surname token."""
    ql = q.lower()
    # Try full name first (most specific)
    for L in leaders:
        if L["name"].lower() in ql:
            return L
    # Then last-token surname match (e.g. "Francis", "Khamenei", "Dalai Lama")
    for L in leaders:
        toks = [t.lower() for t in L["name"].split() if len(t) > 3]
        for t in toks:
            # Require a word-boundary match to avoid "alive" matching "Aleksei"
            if re.search(r"\b" + re.escape(t) + r"\b", ql):
                return L
    return None


def _find_papabile(q: str, papabile: list[dict]) -> Optional[dict]:
    """Match a succession-market question to a papabile by surname."""
    ql = q.lower()
    for p in papabile:
        toks = [t.lower() for t in p["name"].split() if len(t) > 3]
        for t in toks:
            if re.search(r"\b" + re.escape(t) + r"\b", ql):
                return p
    return None


# ─── Edge computation ──────────────────────────────────────────────────────

def attach_edge(market: dict, leaders: list[dict], papabile: list[dict],
                today: date, *, hazard_ratio: float = None) -> dict:
    """Returns a copy of `market` with edge fields attached when matchable.

    Adds keys:
      model_p     — our P(yes) in [0, 1]
      market_p    — same as yes_price (mirror for clarity)
      edge_pp     — (model_p − market_p) * 100, in percentage points
      side        — "YES" if edge > 0, "NO" if < 0, "" if no edge
      model_type  — "actuarial" | "papabile" | "actuarial-vacancy"
      model_basis — human-readable explanation
    """
    if hazard_ratio is None:
        hazard_ratio = historical_leaders.MORTALITY_HAZARD_RATIO_RELIGIOUS

    out = dict(market)
    out["model_p"] = None
    out["market_p"] = market.get("yes_price")
    out["edge_pp"] = None
    out["side"] = ""
    out["model_type"] = None
    out["model_basis"] = ""

    yes = market.get("yes_price")
    if yes is None:
        return out

    question = (market.get("question") or "")
    intent = _classify_intent(question)
    if intent is None:
        return out

    end_date = _parse_iso_date(market.get("end_date") or "")
    if end_date is None or end_date <= today:
        # We need a future target date to compute the model probability.
        return out
    months = _months_between(today, end_date)

    # ─ Conclave / succession ─
    if intent == "succession":
        p = _find_papabile(question, papabile)
        if not p:
            return out
        model_p = p["prior_pct"] / 100.0
        out["model_p"] = round(model_p, 4)
        out["edge_pp"] = round((model_p - yes) * 100, 2)
        out["side"]   = "YES" if model_p > yes else "NO"
        out["model_type"] = "papabile"
        out["model_basis"] = f"papabile prior {p['prior_pct']:.1f}% — {p['rationale']}"
        return out

    # ─ Vacancy = current Pope dies before [date] ─
    if intent == "vacancy":
        francis = next((L for L in leaders if "francis" in L["name"].lower() and "pope" in L["role"].lower()), None)
        if not francis:
            return out
        try:
            age = actuarial.age_on(francis["born"], today)
        except Exception:
            return out
        p_dies = 1.0 - actuarial.survival_prob(age, francis.get("sex", "M"), months, hazard_ratio=hazard_ratio)
        out["model_p"] = round(p_dies, 4)
        out["edge_pp"] = round((p_dies - yes) * 100, 2)
        out["side"]   = "YES" if p_dies > yes else "NO"
        out["model_type"] = "actuarial-vacancy"
        out["model_basis"] = (f"P(Pope Francis dies by {end_date.isoformat()}) "
                              f"= {p_dies*100:.1f}% (age {age:.1f}y, SSA × {hazard_ratio:.2f} HR)")
        return out

    # ─ Mortality / survival of a tracked leader ─
    L = _find_leader(question, leaders)
    if not L or not L.get("born"):
        return out
    try:
        age = actuarial.age_on(L["born"], today)
    except Exception:
        return out
    p_alive = actuarial.survival_prob(age, L.get("sex", "M"), months, hazard_ratio=hazard_ratio)
    if intent == "mortality":
        model_p = 1.0 - p_alive
        basis = f"P({L['name']} dies by {end_date.isoformat()}) = {model_p*100:.1f}%"
    else:  # survival
        model_p = p_alive
        basis = f"P({L['name']} alive on {end_date.isoformat()}) = {model_p*100:.1f}%"
    out["model_p"] = round(model_p, 4)
    out["edge_pp"] = round((model_p - yes) * 100, 2)
    out["side"]   = "YES" if model_p > yes else "NO"
    out["model_type"] = "actuarial"
    out["model_basis"] = basis + f" (age {age:.1f}y, SSA × {hazard_ratio:.2f} HR)"
    return out


def rank_markets_by_edge(markets: list[dict], leaders: list[dict], papabile: list[dict],
                        today: Optional[date] = None) -> list[dict]:
    """Attach edge to every market; return them sorted by absolute edge desc.

    Markets without a matchable model fall to the bottom of the list.
    """
    if today is None:
        today = date.today()
    enriched = [attach_edge(m, leaders, papabile, today) for m in markets]
    # Sort: matched markets by |edge| desc, then unmatched by volume desc
    def _key(m: dict):
        edge = m.get("edge_pp")
        if edge is None:
            return (1, -(m.get("volume") or 0))
        return (0, -abs(edge))
    enriched.sort(key=_key)
    return enriched

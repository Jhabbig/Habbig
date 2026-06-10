"""Map Polymarket disaster questions to a model probability + edge.

Each market is a dict from the gamma API plus our injected ``_event_title``
field. We pick the best-matching model, parse the threshold from the
question text, evaluate the model, and attach ``_model_p``, ``_implied_p``,
``_edge_pp``, ``_rationale``, ``_model_used`` to the market dict.

Models supported:

  1. Atlantic named-storm count: "Will there be at least N named storms?"
  2. Hurricane count                              ("at least N hurricanes")
  3. Major hurricane (Cat3+) count                ("at least N major")
  4. M{x}+ earthquake count by year-end           ("at least N M5+ quakes")
  5. Wildfire-event count by year-end (EONET)     ("at least N wildfires")
  6. Wildfire acres burned by year-end (NIFC)     ("X+ acres burned in 2026")
  7. US tornado count by year-end                 ("at least N tornadoes")
  8. FEMA major-disaster declaration count        ("FEMA declares 60+ disasters")
  9. Volcano-erupted-in-week markets              ("Volcano X erupts in 2026")

Each model is gated on keywords in the title. The threshold is parsed via
regex with three patterns supported:
    "at least N"           -> P(year-end >= N)
    "fewer than N"         -> 1 - P(year-end >= N)
    "between N and M"      -> P(N <= year-end <= M)

Where the threshold can't be parsed or no model fits, the market is still
returned with ``_model_p = None``.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from .kelly import position_size
from .negbin import ALPHA, nb_between, nb_cdf_at_least, nb_quantile_band
from .poisson import p_at_least, p_between

# ─── Threshold parsers ─────────────────────────────────────────────────────────

# Unit suffix must end at a word boundary so "60 major" doesn't grab "m"
# and "M7+" doesn't get scaled. Single-letter "m"/"k" aliases are only
# accepted when the user actually wrote a digit + unit (e.g. "5m acres").
_UNIT = r"(?:(million|thousand|billion)\b|(?<=\d)(m|k|b)\b)"

_RE_AT_LEAST = re.compile(
    r"(?:at\s+least|more\s+than|over|exceed[a-z]*|>=?\s*|\bat\s+or\s+above|reach[a-z]*)\s*"
    r"(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*" + _UNIT + r"?", re.I)
_RE_FEWER = re.compile(
    r"(?:fewer\s+than|less\s+than|under|<=?\s*|below|no\s+more\s+than)\s*"
    r"(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*" + _UNIT + r"?", re.I)
_RE_BETWEEN = re.compile(
    r"between\s*(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*" + _UNIT + r"?\s*"
    r"(?:&|and|to|-)\s*"
    r"(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*" + _UNIT + r"?", re.I)
_RE_EXACT = re.compile(
    r"\bexactly\s*(\d{1,4})\s*" + _UNIT + r"?", re.I)
# "5 million acres" / "8m acres burn" - implicit at-least when no qualifier
_RE_BARE_NUMBER_UNIT = re.compile(
    r"\b(\d{1,4}(?:,\d{3})*(?:\.\d+)?)\s*" + _UNIT + r"?\s*acres?\b", re.I)
_RE_MAGNITUDE = re.compile(r"\bm\s*(\d(?:\.\d)?)\b|magnitude\s*(\d(?:\.\d)?)", re.I)


def _scale(value: str, full_suffix: Optional[str], short_suffix: Optional[str]) -> float:
    n = float(value.replace(",", ""))
    s = (full_suffix or short_suffix or "").lower()
    if s in {"million", "m"}:
        return n * 1_000_000
    if s in {"thousand", "k"}:
        return n * 1_000
    if s in {"billion", "b"}:
        return n * 1_000_000_000
    return n


def _at_least(text: str) -> Optional[float]:
    m = _RE_AT_LEAST.search(text)
    if m:
        try:
            return _scale(m.group(1), m.group(2), m.group(3))
        except ValueError:
            return None
    # Fallback: bare "X million acres" implies at-least when paired with an
    # acres-typed market.
    m = _RE_BARE_NUMBER_UNIT.search(text)
    if m:
        try:
            return _scale(m.group(1), m.group(2), m.group(3))
        except ValueError:
            return None
    return None


def _fewer_than(text: str) -> Optional[float]:
    m = _RE_FEWER.search(text)
    if m:
        try:
            return _scale(m.group(1), m.group(2), m.group(3))
        except ValueError:
            return None
    return None


def _between(text: str) -> Optional[tuple[float, float]]:
    m = _RE_BETWEEN.search(text)
    if not m:
        return None
    try:
        lo = _scale(m.group(1), m.group(2), m.group(3))
        hi = _scale(m.group(4), m.group(5), m.group(6))
    except ValueError:
        return None
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _exact(text: str) -> Optional[int]:
    m = _RE_EXACT.search(text)
    if m:
        try:
            return int(_scale(m.group(1), m.group(2), m.group(3)))
        except ValueError:
            return None
    return None


# ─── Implied-price helper ──────────────────────────────────────────────────────

def _parse_implied(market: dict) -> Optional[float]:
    for key in ("lastTradePrice", "bestBid", "bestAsk"):
        v = market.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return f
    return None


# ─── Topic detectors ───────────────────────────────────────────────────────────

def _is_atlantic_storm_market(tl: str) -> bool:
    if "tornado" in tl or "earthquake" in tl:
        return False
    return any(k in tl for k in ("named storm", "atlantic", "hurricane",
                                 "tropical storm", "tropical cyclone"))


def _is_hurricane_market(tl: str) -> bool:
    return "hurricane" in tl and "named storm" not in tl


def _is_major_hurricane_market(tl: str) -> bool:
    return ("major hurricane" in tl or "category 3" in tl or "cat 3" in tl
            or "category 4" in tl or "cat 4" in tl
            or "category 5" in tl or "cat 5" in tl)


def _is_wildfire_count_market(tl: str) -> bool:
    if "acre" in tl or "million" in tl:  # acres-based, scored elsewhere
        return False
    return any(k in tl for k in ("wildfire", "wild fire", "forest fire", "bushfire"))


def _is_wildfire_acres_market(tl: str) -> bool:
    has_acres = "acre" in tl
    has_fire_keyword = any(
        k in tl for k in ("wildfire", "wild fire", "forest fire", "bushfire", "fire season"))
    has_acres_burn = has_acres and "burn" in tl
    return has_acres_burn or (has_acres and has_fire_keyword)


def _is_quake_market(tl: str) -> bool:
    return any(k in tl for k in ("earthquake", "magnitude", "richter"))


def _is_tornado_market(tl: str) -> bool:
    return "tornado" in tl or "twister" in tl


def _is_fema_market(tl: str) -> bool:
    return "fema" in tl or "disaster declaration" in tl


def _is_volcano_market(tl: str) -> bool:
    return any(k in tl for k in ("volcano", "volcanic", "eruption", "erupt"))


def _quake_threshold_from_title(tl: str) -> Optional[float]:
    m = _RE_MAGNITUDE.search(tl)
    if not m:
        return None
    try:
        return float(m.group(1) or m.group(2))
    except (TypeError, ValueError):
        return None


# ─── Scorers ───────────────────────────────────────────────────────────────────

def _score_count_market(text: str, ytd: int, lam: float, label: str,
                          alpha_key: str = "") -> tuple[Optional[float], str]:
    """Count-market scorer with negative-binomial tail.

    Tries 'between N and M' first, then 'at least N', then 'fewer than N'.
    Parses thresholds with _at_least/_between/_fewer_than which already
    handle the suffix "million/thousand" for unit-scaled markets.

    Uses the negative-binomial tail with empirical dispersion ``alpha``
    when ``alpha_key`` is supplied (recommended for tornadoes / storms /
    wildfires which are systematically overdispersed). Falls back to plain
    Poisson when ``alpha_key`` is unrecognised or 0.
    """
    alpha = ALPHA.get(alpha_key, 0.0)
    family = f"NB(mu, alpha={alpha:.3f})" if alpha > 0 else f"Poisson(lambda)"

    bw = _between(text)
    if bw is not None:
        lo, hi = bw
        try:
            ilo = int(round(lo - ytd))
            ihi = int(round(hi - ytd))
        except (TypeError, ValueError):
            return None, ""
        ilo = max(ilo, 0)
        ihi = max(ihi, 0)
        if alpha > 0:
            p = nb_between(ilo, ihi, lam, alpha)
        else:
            p = p_between(lam, ilo, ihi)
        if p is None:
            return None, ""
        return p, f"{label} YTD {ytd} + {family}; P({lo:.0f} <= total <= {hi:.0f})"
    n = _at_least(text)
    if n is not None:
        needed = max(int(round(n - ytd)), 0)
        if alpha > 0:
            p = nb_cdf_at_least(needed, lam, alpha)
        else:
            p = p_at_least(lam, needed)
        if p is None:
            return None, ""
        return p, f"{label} YTD {ytd} + {family}; need {needed} more for >= {int(n)}"
    n = _fewer_than(text)
    if n is not None:
        needed = max(int(round(n - ytd)), 0)
        if alpha > 0:
            p = nb_cdf_at_least(needed, lam, alpha)
        else:
            p = p_at_least(lam, needed)
        if p is None:
            return None, ""
        return 1.0 - p, f"{label} YTD {ytd} + {family}; P(total < {int(n)})"
    return None, ""


def _score_storm_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    if not proj or proj.get("error"):
        return None, ""
    ytd = proj.get("active_named_storms_ytd_lower_bound") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    return _score_count_market(title, ytd, lam, "Atlantic named storms:",
                                alpha_key="atlantic_named_storms")


def _score_hurricane_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    """Hurricanes are about half of named-storms historically (50%)."""
    if not proj or proj.get("error"):
        return None, ""
    ytd_named = proj.get("active_named_storms_ytd_lower_bound") or 0
    lam_named = proj.get("lambda_remaining")
    if lam_named is None:
        return None, ""
    ytd_h = int(round(ytd_named * 0.5))
    lam_h = lam_named * 0.5
    return _score_count_market(title, ytd_h, lam_h, "Atlantic hurricanes (~50% of named):",
                                alpha_key="atlantic_hurricanes")


def _score_major_hurricane_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    """Major hurricanes (Cat 3+) are ~21% of named storms historically."""
    if not proj or proj.get("error"):
        return None, ""
    lam_named = proj.get("lambda_remaining")
    ytd_named = proj.get("active_named_storms_ytd_lower_bound") or 0
    if lam_named is None:
        return None, ""
    return _score_count_market(title, int(round(ytd_named * 0.21)),
                                lam_named * 0.21, "Atlantic major hurricanes (~21% of named):",
                                alpha_key="atlantic_major_hurricanes")


def _score_quake_market(title: str, projections_by_mag: dict) -> tuple[Optional[float], str]:
    mag = _quake_threshold_from_title(title)
    if mag is None:
        return None, ""
    available = sorted(projections_by_mag.keys())
    if not available:
        return None, ""
    nearest = min(available, key=lambda m: abs(m - mag))
    proj = projections_by_mag.get(nearest)
    if not proj or proj.get("error"):
        return None, ""
    ytd = proj.get("ytd_count") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    alpha_key = (
        "global_m5" if nearest <= 5.5
        else "global_m6" if nearest <= 6.5
        else "global_m7"
    )
    return _score_count_market(title, ytd, lam, f"M{nearest}+ quakes:",
                                alpha_key=alpha_key)


def _score_wildfire_count_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    if not proj or proj.get("error"):
        return None, ""
    ytd = proj.get("ytd_count") or 0
    lam = proj.get("lambda_remaining")
    if lam is None:
        return None, ""
    return _score_count_market(title, ytd, lam, "EONET wildfires:",
                                alpha_key="wildfire_count")


def _score_wildfire_acres_market(title: str, acres_proj: dict) -> tuple[Optional[float], str]:
    """For markets like 'Will 5+ million acres burn in 2026?' use the NIFC
    acres projection's Normal(mu, sigma) tail."""
    if not acres_proj or acres_proj.get("error"):
        return None, ""
    mu = acres_proj.get("projected_year_end_acres")
    sigma = acres_proj.get("projection_sigma_acres")
    if mu is None or not sigma:
        return None, ""
    sigma = max(float(sigma), 50_000.0)

    def _normal_complement(thr: float) -> float:
        z = (thr - mu) / sigma
        return 0.5 * math.erfc(z / math.sqrt(2))

    bw = _between(title)
    if bw is not None:
        lo, hi = bw
        return (_normal_complement(lo) - _normal_complement(hi),
                f"NIFC acres: N(mu={int(mu):,}, sigma={int(sigma):,}); P({int(lo):,} <= acres <= {int(hi):,})")
    n = _at_least(title)
    if n is not None:
        return (_normal_complement(n),
                f"NIFC acres: N(mu={int(mu):,}, sigma={int(sigma):,}); P(acres >= {int(n):,})")
    n = _fewer_than(title)
    if n is not None:
        return (1.0 - _normal_complement(n),
                f"NIFC acres: N(mu={int(mu):,}, sigma={int(sigma):,}); P(acres < {int(n):,})")
    return None, ""


def _score_tornado_market(title: str, proj: dict) -> tuple[Optional[float], str]:
    if not proj or proj.get("error"):
        return None, ""
    mu = proj.get("projected_year_end_count")
    sigma = proj.get("year_end_sigma")
    if mu is None or sigma is None:
        # Fallback: use Poisson approx with lam_remaining if year_end_sigma absent.
        lam = proj.get("lambda_remaining")
        if mu is None or lam is None:
            return None, ""
        sigma = math.sqrt(max(float(lam), 1.0))
    sigma = float(sigma)

    def _normal_complement(thr: float) -> float:
        z = (thr - mu) / sigma
        return 0.5 * math.erfc(z / math.sqrt(2))

    bw = _between(title)
    if bw is not None:
        lo, hi = bw
        return (_normal_complement(lo) - _normal_complement(hi),
                f"US tornadoes climo: N(mu={int(mu)}, sigma={sigma:.0f}); P({int(lo)} <= count <= {int(hi)})")
    n = _at_least(title)
    if n is not None:
        return (_normal_complement(n),
                f"US tornadoes climo: N(mu={int(mu)}, sigma={sigma:.0f}); P(count >= {int(n)})")
    n = _fewer_than(title)
    if n is not None:
        return (1.0 - _normal_complement(n),
                f"US tornadoes climo: N(mu={int(mu)}, sigma={sigma:.0f}); P(count < {int(n)})")
    return None, ""


def _score_fema_market(title: str, fema_proj: dict) -> tuple[Optional[float], str]:
    if not fema_proj or fema_proj.get("error"):
        return None, ""
    ytd = fema_proj.get("ytd_major_disasters_dr") or 0
    lam = fema_proj.get("lambda_dr_remaining")
    if lam is None:
        return None, ""
    return _score_count_market(title, ytd, lam, "FEMA major-disaster (DR) declarations:",
                                alpha_key="fema_dr")


def _polymarket_deep_link(market: dict) -> Optional[str]:
    """Build a deep-link URL to the Polymarket market on polymarket.com."""
    slug = market.get("slug") or market.get("_event_slug")
    if not slug:
        return None
    if slug.startswith("http"):
        return slug
    return f"https://polymarket.com/event/{slug}"


# ─── Public entry point ───────────────────────────────────────────────────────

def enrich_markets(
    markets: list[dict],
    *,
    storm_proj: Optional[dict] = None,
    quake_projections: Optional[dict] = None,
    wildfire_count_proj: Optional[dict] = None,
    wildfire_acres_proj: Optional[dict] = None,
    tornado_proj: Optional[dict] = None,
    fema_proj: Optional[dict] = None,
) -> list[dict]:
    """Attach _model_p / _implied_p / _edge_pp / _rationale / _model_used to each market."""
    out: list[dict] = []
    for m in markets:
        title = ((m.get("_event_title") or "") + " " + (m.get("question") or "")).strip()
        tl = title.lower()
        implied = _parse_implied(m)
        model_p: Optional[float] = None
        rationale = ""
        model_used = ""

        # The order matters: more-specific topics first, then general fallbacks.
        if _is_major_hurricane_market(tl) and storm_proj:
            model_p, rationale = _score_major_hurricane_market(tl, storm_proj)
            if model_p is not None:
                model_used = "atlantic_major_hurricanes"

        if model_p is None and _is_hurricane_market(tl) and storm_proj:
            model_p, rationale = _score_hurricane_market(tl, storm_proj)
            if model_p is not None:
                model_used = "atlantic_hurricanes"

        if model_p is None and _is_atlantic_storm_market(tl) and storm_proj:
            model_p, rationale = _score_storm_market(tl, storm_proj)
            if model_p is not None:
                model_used = "atlantic_named_storms"

        if model_p is None and _is_quake_market(tl) and quake_projections:
            model_p, rationale = _score_quake_market(tl, quake_projections)
            if model_p is not None:
                model_used = "global_quakes"

        if model_p is None and _is_wildfire_acres_market(tl) and wildfire_acres_proj:
            model_p, rationale = _score_wildfire_acres_market(tl, wildfire_acres_proj)
            if model_p is not None:
                model_used = "nifc_acres"

        if model_p is None and _is_wildfire_count_market(tl) and wildfire_count_proj:
            model_p, rationale = _score_wildfire_count_market(tl, wildfire_count_proj)
            if model_p is not None:
                model_used = "eonet_wildfires"

        if model_p is None and _is_tornado_market(tl) and tornado_proj:
            model_p, rationale = _score_tornado_market(tl, tornado_proj)
            if model_p is not None:
                model_used = "us_tornadoes"

        if model_p is None and _is_fema_market(tl) and fema_proj:
            model_p, rationale = _score_fema_market(tl, fema_proj)
            if model_p is not None:
                model_used = "fema_dr"

        # Clamp to [0,1] - Poisson approximation can drift slightly outside
        if model_p is not None:
            model_p = max(0.0, min(1.0, model_p))

        edge_pp: Optional[float] = None
        if implied is not None and model_p is not None:
            edge_pp = round((model_p - implied) * 100, 1)

        kelly = position_size(model_p, implied) if model_p is not None and implied is not None else None

        out.append({
            **m,
            "_implied_p": implied,
            "_model_p": round(model_p, 3) if model_p is not None else None,
            "_edge_pp": edge_pp,
            "_rationale": rationale,
            "_model_used": model_used,
            "_kelly": kelly,
            "_trade_url": _polymarket_deep_link(m),
        })
    # Sort: scored markets first (by absolute edge desc), unscored last
    out.sort(key=lambda r: (
        0 if r.get("_edge_pp") is not None else 1,
        -abs(r.get("_edge_pp") or 0),
    ))
    return out


if __name__ == "__main__":
    # Smoke test - exercise each model branch with synthetic input
    import json

    storm_proj = {
        "lambda_remaining": 4.0,
        "active_named_storms_ytd_lower_bound": 10,
        "days_remaining_season": 30,
    }
    quake_projs = {
        5.0: {"ytd_count": 700, "lambda_remaining": 250.0, "days_remaining": 90},
        6.0: {"ytd_count": 70, "lambda_remaining": 25.0, "days_remaining": 90},
        7.0: {"ytd_count": 7, "lambda_remaining": 2.5, "days_remaining": 90},
    }
    wildfire_count = {"ytd_count": 80, "lambda_remaining": 20.0, "days_remaining": 90}
    wildfire_acres = {"projected_year_end_acres": 7_000_000, "projection_sigma_acres": 2_000_000}
    tornado_proj = {"projected_year_end_count": 1250, "lambda_remaining": 400}
    fema_proj = {"ytd_major_disasters_dr": 30, "lambda_dr_remaining": 25}

    markets = [
        {"_event_title": "Atlantic 2026 forecast",
         "question": "At least 14 Atlantic named storms in 2026", "lastTradePrice": "0.65"},
        {"_event_title": "Hurricane season",
         "question": "More than 7 Atlantic hurricanes in 2026", "lastTradePrice": "0.50"},
        {"_event_title": "M7+ quakes",
         "question": "At least 18 M7+ earthquakes in 2026", "lastTradePrice": "0.40"},
        {"_event_title": "Wildfires",
         "question": "Will 8 million acres burn in 2026?", "lastTradePrice": "0.55"},
        {"_event_title": "Tornado count",
         "question": "Between 1100 and 1400 tornadoes in 2026", "lastTradePrice": "0.30"},
        {"_event_title": "FEMA",
         "question": "FEMA declares at least 60 major disasters in 2026", "lastTradePrice": "0.70"},
    ]
    enriched = enrich_markets(
        markets,
        storm_proj=storm_proj,
        quake_projections=quake_projs,
        wildfire_count_proj=wildfire_count,
        wildfire_acres_proj=wildfire_acres,
        tornado_proj=tornado_proj,
        fema_proj=fema_proj,
    )
    for r in enriched:
        print(f"{r['_model_used'] or 'unscored':24s}  edge={r['_edge_pp']}  p={r['_model_p']}  ->  {r['_rationale']}")

"""Shared rule-based outcome classifier — maps an FOMC market question
(or Kalshi market title/subtitle) to a bucket label in our standard
vocabulary: ``cut50``, ``cut25``, ``hold``, ``hike25``, ``hike50``, etc.

Used by both :mod:`polymarket_client` and :mod:`kalshi_client` so the same
buckets line up across venues for the cross-venue arbitrage view.

Three matching styles:

  1. **Hold phrases** — "hold rates steady", "no change", "unchanged" → ``hold``
  2. **Verb + bps** — "cut by 25 bps", "raises rates 25bp", "hike 50 basis points"
  3. **Level-based** (Kalshi-style) — "Fed funds rate at 4.25%-4.50%". Requires
     the current effective rate so we can compute the delta and round to the
     nearest 25-bp bucket.

All matching is case-insensitive and tolerant of common inflections
(``cut/cuts/cutting``, ``raise/raises/raised``).
"""

from __future__ import annotations

import re

# Verb stems with all common inflections — Polymarket and Kalshi both phrase
# moves multiple ways: "cut by 25 bps", "raises rates 25bps", "Fed cutting 50
# basis points", "hiked 25bp".
_CUT_VERB = (
    r"(?:cut|cuts|cutting|decrease|decreases|decreased|"
    r"reduce|reduces|reduced|lower|lowers|lowered)"
)
_HIKE_VERB = (
    r"(?:hike|hikes|hiked|hiking|increase|increases|increased|"
    r"raise|raises|raised|raising)"
)
_VERB = f"(?:{_CUT_VERB}|{_HIKE_VERB})"
_BPS = r"(?:bps|bp|basis\s*points?)"

# `>N` / `more than N` matches range markets we deliberately don't map onto
# single-step buckets — they aggregate multiple Polymarket buckets and need a
# separate cumulative-probability comparison. Skip them in v1.
_INEQ = r"(?:>|>=|≥|more\s+than|at\s+least)"
_VERB_BPS_RX = re.compile(rf"({_VERB})\b[^.\n]{{0,40}}?({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}", re.I)
_BPS_VERB_RX = re.compile(rf"({_INEQ}\s*)?(\d{{1,3}})\s*{_BPS}[^.\n]{{0,30}}?({_VERB})", re.I)
_HOLD_RX = re.compile(
    r"\b(hold|no change|unchanged|leave (?:rates? )?(?:the same|alone)|fed maintains rate)\b",
    re.I,
)
_CUT_RX = re.compile(_CUT_VERB, re.I)

# Level-based patterns:
#   "4.25-4.50%" or "4.25%-4.50%" or "between 4.25 and 4.50"
#   "at 4.50%"
_RANGE_RX = re.compile(
    r"(\d+\.\d{1,2})\s*%?\s*[-–to]+\s*(\d+\.\d{1,2})\s*%", re.I,
)
_SINGLE_RX = re.compile(r"(\d+\.\d{1,2})\s*%")


def classify_delta(text: str) -> str | None:
    """Try to map ``text`` to a bucket via verb-and-bps matching.

    Returns one of ``hold``, ``cut25``, ``cut50``, ``hike25``, … or
    ``None`` if no clean match. Hold takes priority over verb matches.
    """
    q = text or ""
    if _HOLD_RX.search(q):
        return "hold"
    m = _VERB_BPS_RX.search(q)
    if m:
        verb, ineq, bps = m.group(1), m.group(2), int(m.group(3))
        # Skip range markets like ">25bps" — they aggregate buckets and need a
        # different comparison than single-step implied probabilities.
        if ineq:
            return None
        # Kalshi phrases hold as "Hike rates by 0bps" — N=0 ⇒ hold regardless.
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    m = _BPS_VERB_RX.search(q)
    if m:
        ineq, bps, verb = m.group(1), int(m.group(2)), m.group(3)
        if ineq:
            return None
        if bps == 0:
            return "hold"
        return f"{'cut' if _CUT_RX.fullmatch(verb) else 'hike'}{bps}"
    return None


def classify_level(text: str, current_rate: float | None) -> str | None:
    """Try to map a level-based question (e.g. ``"Fed target rate at 4.25%-4.50%"``)
    to a bucket. Needs ``current_rate`` (in percent) to compute the delta.

    Rounds the implied delta to the nearest 25 bp. Returns ``None`` when no
    level can be parsed or when ``current_rate`` is unavailable.
    """
    if current_rate is None or not text:
        return None
    text_l = text.lower()
    # Fed "target range" convention: "4.25-4.50%" denotes the band the target
    # rate sits in. The conventional shorthand uses the **upper bound** as
    # the headline rate (so "current rate is 4.50%" means range 4.25-4.50%).
    # We pick the **lower bound** of the question band, then compare to the
    # caller's `current_rate`. With a 25-bp cut from 4.50%, the new range is
    # 4.00-4.25%; "rate at 4.00-4.25%" then has lower=4.00, current=4.50,
    # delta=-0.50 → cut50. Lower-bound matching corresponds to a single-step
    # cut/hold/hike interpretation aligned with Polymarket's delta vocabulary.
    m = _RANGE_RX.search(text_l)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        target = lo
    else:
        m = _SINGLE_RX.search(text_l)
        if not m:
            return None
        target = float(m.group(1))
    delta_pct = target - current_rate
    delta_bps = round(delta_pct * 100 / 25) * 25
    if delta_bps == 0:
        return "hold"
    return f"{'cut' if delta_bps < 0 else 'hike'}{abs(delta_bps)}"


def classify(text: str, current_rate: float | None = None) -> str | None:
    """Combined: try delta-style first, fall back to level-style."""
    bucket = classify_delta(text)
    if bucket:
        return bucket
    return classify_level(text, current_rate)


# --- Self-test --------------------------------------------------------------

_FIXTURES = [
    # (text, current_rate, expected)
    ("Will the Fed cut rates by 25 bps in April 2026?", 4.50, "cut25"),
    ("Fed rate decision: 50 bp hike in June?", 4.50, "hike50"),
    ("Will the FOMC hold rates steady in May?", 4.50, "hold"),
    ("Federal Reserve raises rates by 25 basis points", 4.50, "hike25"),
    ("Federal funds rate: no change in April 2026", 4.50, "hold"),
    # Kalshi phrasings (live in their KXFEDDECISION series)
    ("Will the Federal Reserve Cut rates by 25bps at their June 2026 meeting?", 4.50, "cut25"),
    ("Will the Federal Reserve Hike rates by 0bps at their June 2026 meeting?", 4.50, "hold"),
    ("Will the Federal Reserve Hike rates by 25bps at their June 2026 meeting?", 4.50, "hike25"),
    # Range markets — deliberately not classified (aggregate of cut50/cut75/...)
    ("Will the Federal Reserve Cut rates by >25bps at their June 2026 meeting?", 4.50, None),
    ("Will the Federal Reserve Hike rates by more than 25 bps in June?", 4.50, None),
    # Level-based (alternate Kalshi style)
    ("Fed funds target rate at 4.25%-4.50% after April 2026 meeting", 4.50, "cut25"),
    ("Target rate at 4.50%-4.75% after April 2026 meeting", 4.50, "hold"),
    ("Fed funds rate above 5.00%", 4.50, "hike50"),
    # Negatives
    ("Bitcoin to $200k by year-end", None, None),
    ("Trump approval rating", 4.50, None),
]


if __name__ == "__main__":
    for text, rate, expected in _FIXTURES:
        got = classify(text, rate)
        ok = "✓" if got == expected else "✗"
        print(f"  {ok} expected={expected!r:8s}  got={got!r:8s}  | {text}")

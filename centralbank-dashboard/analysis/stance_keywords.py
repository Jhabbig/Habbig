"""Hawkish ↔ dovish keyword dictionary for rule-based statement scoring.

Weights are integer hand-set values:
    +N  hawkish (favor tighter policy)
    -N  dovish  (favor looser policy)
    |N| larger  ⇒ stronger / less ambiguous signal

Matching is case-insensitive whole-phrase. The scorer normalizes by sentence
count so longer statements aren't mechanically more extreme.

This list intentionally covers the *vocabulary central bankers actually use*,
not generic financial jargon. Phrases were drawn from Fed/ECB/BoE statements
between roughly 2018–2025 and from public Fed Bayesian-NLP papers.

To extend: add a phrase to the appropriate dict; re-run smoke tests.
"""

from __future__ import annotations

# --- HAWKISH (positive weights) ---------------------------------------------
HAWKISH: dict[str, int] = {
    # Strong hawkish — explicit tightening / inflation alarm
    "tighten": 3,
    "tightening": 3,
    "restrictive": 3,
    "more restrictive": 4,
    "sufficiently restrictive": 3,
    "elevated inflation": 3,
    "above target": 2,
    "inflationary pressures": 3,
    "overheating": 4,
    "wage pressures": 2,
    "robust demand": 2,
    "strong labor market": 2,
    "tight labor market": 3,

    # Forward guidance hawkish
    "additional firming": 4,
    "further increases": 3,
    "more rate increases": 3,
    "higher for longer": 3,
    "further tightening": 3,
    "vigilant": 2,
    "remain attentive": 2,
    "upside risks": 2,
    "upside surprise": 2,

    # Hawkish softer
    "above 2 percent": 1,
    "above 2%": 1,
    "elevated": 1,
    "firming": 2,
    "ample progress": 1,  # signals confidence to maintain restriction
    "pause": 1,           # in context of restrictive stance
}

# --- DOVISH (negative weights) ----------------------------------------------
DOVISH: dict[str, int] = {
    # Strong dovish — explicit easing / weak demand
    "accommodative": -3,
    "ease": -2,
    "easing": -2,
    "additional accommodation": -4,
    "support growth": -2,
    "support the economy": -2,
    "downside risks": -2,
    "moderation": -1,
    "softening": -2,
    "softer": -2,
    "subdued": -2,
    "weakening": -2,
    "cooling": -2,
    "disinflation": -3,
    "well anchored": -1,
    "moderating": -1,
    "below target": -1,

    # Forward guidance dovish
    "patient": -1,
    "appropriate to reduce": -3,
    "rate cuts": -3,
    "cut rates": -3,
    "lower the target": -3,
    "begin to ease": -3,
    "consider cuts": -2,
    "cautious approach": -1,
    "remain patient": -1,

    # Dovish softer
    "slowdown": -1,
    "softening labor": -2,
    "below 2 percent": -1,
    "muted": -1,
    "transitory": -1,    # famously contested — light weight
    "data dependent": -1,  # mild dovish bias when paired with restrictive stance
}

# --- NEUTRAL / context phrases (not scored) ---------------------------------
# Tracked for transparency in matched-phrase output but contribute 0.
NEUTRAL: set[str] = {
    "balanced",
    "uncertain",
    "monitor closely",
    "wait and see",
    "data-dependent",
    "carefully assess",
}


def all_phrases() -> dict[str, int]:
    out: dict[str, int] = {}
    out.update(HAWKISH)
    out.update(DOVISH)
    return out

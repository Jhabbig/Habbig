"""Per-regulator stance dictionaries — v0.7.

Unlike the v0.1 type classifier (one orthogonal multi-class scorer) or
the v0.4 topics (multi-tag orthogonal), stance is a **per-regulator
single axis**. Each body has a different "philosophical pole" in
policy:

  - SEC:  pro-enforcement (+)  ↔  light-touch (–)
  - FCA:  pro-innovation (+)   ↔  consumer-first (–)
  - ESMA: prescriptive (+)     ↔  principles-based (–)

The pole choice is deliberately asymmetric — picking the *real* axis a
given regulator is contested on, not a generic "strict ↔ lax". An SEC
speech could be both pro-enforcement AND consumer-friendly; only the
enforcement axis tells you which way SEC culture is moving.

Same scoring mechanics as `centralbank-dashboard/analysis/stance_scorer.py`:
positive-weighted phrases push score toward the positive pole, negative-
weighted phrases push it toward the negative pole. The scorer normalizes
by sentence count so a long speech with one strong phrase doesn't drown
in noise.

To extend / tune:
  - Add phrases to the relevant `<reg>_POSITIVE` / `<reg>_NEGATIVE` dict
  - Higher absolute weight = stronger signal (range: ±1..4)
  - Avoid single-word phrases that appear in unrelated contexts — prefer
    multi-word forms like "consumer protection" over "consumer"
  - Re-run `python3 -m analysis.stance` against the fixtures

To add a new regulator: append an entry to `AXES` below — done.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StanceAxis:
    """One regulator's policy axis. Weight sign follows centralbank
    convention: positives push to the positive label, negatives push to
    the negative label, both contribute to raw_score directly."""
    code: str
    name: str
    positive_label: str       # short display, e.g. "PRO_ENFORCEMENT"
    negative_label: str       # short display, e.g. "LIGHT_TOUCH"
    positive_long: str        # full description
    negative_long: str
    positive: dict[str, int]  # phrase → positive int weight
    negative: dict[str, int]  # phrase → negative int weight (already negative)
    threshold: float = 0.3    # |norm_score| above this fires the labeled bucket


# ── SEC ────────────────────────────────────────────────────────────────────
_SEC_POSITIVE = {
    "robust enforcement": 4,
    "vigorous enforcement": 4,
    "rigorous oversight": 3,
    "investor protection": 2,
    "investor harm": 3,
    "hold accountable": 3,
    "accountability": 2,
    "deter misconduct": 4,
    "rooting out fraud": 4,
    "regulatory failures": 2,
    "necessary protections": 2,
    "regulatory gaps": 2,
    "wells notice": 2,
    "civil penalty": 2,
    "egregious": 3,
    "fraudulent conduct": 3,
    "market manipulation": 2,
    "tougher rules": 3,
}
_SEC_NEGATIVE = {
    "regulatory burden": -3,
    "compliance cost": -2,
    "compliance burden": -3,
    "innovation-friendly": -3,
    "pro-growth": -2,
    "deregulatory": -4,
    "regulatory overreach": -4,
    "burden on issuers": -3,
    "streamline regulations": -3,
    "streamline disclosure": -2,
    "modernize the rules": -2,
    "barriers to capital formation": -3,
    "ease compliance": -2,
    "principles-based": -2,  # also an ESMA axis; here it skews light-touch
    "tailored disclosure": -1,
    "capital formation": -1,
}

# ── FCA ────────────────────────────────────────────────────────────────────
_FCA_POSITIVE = {
    "innovation": 2,
    "innovative": 2,
    "competition": 2,
    "competitive markets": 3,
    "fintech": 2,
    "sandbox": 3,
    "regulatory sandbox": 3,
    "remove barriers": 3,
    "growth and competitiveness": 4,  # FCA's statutory secondary objective
    "secondary objective": 3,
    "international competitiveness": 4,
    "regulatory burden": -1,  # mild — paradoxically pro-innovation rhetoric uses this
}
_FCA_NEGATIVE = {
    "consumer protection": -3,
    "consumer harm": -3,
    "vulnerable consumers": -3,
    "consumer duty": -3,
    "fair value": -2,
    "fair outcomes": -2,
    "good outcomes": -2,
    "consumer outcomes": -3,
    "redress": -2,
    "treating customers fairly": -3,
    "tcf": -2,
    "appropriate redress": -2,
    "scam": -2,
    "scams": -2,
    "exploitation": -2,
    "predatory": -3,
}

# ── ESMA ───────────────────────────────────────────────────────────────────
_ESMA_POSITIVE = {
    "regulatory technical standards": 3,
    "implementing technical standards": 3,
    "binding": 2,
    "binding guidelines": 4,
    "harmonisation": 3,
    "harmonization": 3,
    "single rulebook": 4,
    "uniform application": 3,
    "common standards": 3,
    "common methodology": 2,
    "convergence": 2,
    "supervisory convergence": 4,
    "level playing field": 2,
    "consistent application": 2,
    "co-ordinated approach": 2,
}
_ESMA_NEGATIVE = {
    "principles-based": -3,
    "proportionate": -2,
    "proportionality": -2,
    "national discretion": -3,
    "flexibility": -2,
    "outcomes-based": -3,
    "risk-based approach": -3,
    "national competent authorities": -1,
    "subsidiarity": -3,
    "case by case": -2,
    "case-by-case": -2,
    "tailored to national": -3,
}

# ── BoE — pro-stability ↔ pro-growth ───────────────────────────────────────
_BOE_POSITIVE = {
    "financial stability": 4,
    "macroprudential": 4,
    "systemic risk": 3,
    "resilience": 2,
    "capital buffer": 3,
    "capital requirements": 2,
    "stress test": 2,
    "countercyclical": 3,
    "robust supervision": 3,
    "prudential": 2,
    "vigilant": 1,
    "tail risk": 2,
}
_BOE_NEGATIVE = {
    "competitiveness": -3,
    "growth and competitiveness": -4,
    "secondary objective": -3,
    "innovation": -1,
    "regulatory burden": -2,
    "remove barriers": -3,
    "pro-growth": -3,
    "supporting growth": -2,
    "competition": -1,
    "international competitiveness": -3,
}

# ── CFTC — pro-enforcement ↔ light-touch ───────────────────────────────────
_CFTC_POSITIVE = {
    "enforcement action": 3,
    "civil penalty": 3,
    "manipulation": 2,
    "market manipulation": 3,
    "fraud": 2,
    "fraudulent": 2,
    "disgorgement": 3,
    "obtains judgment": 3,
    "wash trading": 2,
    "spoofing": 3,
    "egregious": 3,
    "rigorous oversight": 3,
    "vigorous enforcement": 4,
    "charges": 2,
    "settles": 2,
    "swap data": 1,
    "position limits": 2,
}
_CFTC_NEGATIVE = {
    "regulatory clarity": -3,
    "innovation-friendly": -3,
    "principles-based": -2,
    "pilot program": -2,
    "regulatory sandbox": -3,
    "no-action letter": -2,
    "deregulatory": -3,
    "compliance burden": -2,
    "modernize the rules": -2,
    "tailored": -1,
}

# ── Fed — hawkish ↔ dovish ─────────────────────────────────────────────────
# Mirrors centralbank-dashboard/analysis/stance_keywords.py for consistency
# with the existing monetary-policy stance signal.
_FED_POSITIVE = {
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
    "tight labor market": 3,
    "additional firming": 4,
    "further increases": 3,
    "higher for longer": 3,
    "further tightening": 3,
    "vigilant": 2,
    "remain attentive": 2,
    "upside risks": 2,
}
_FED_NEGATIVE = {
    "accommodative": -3,
    "ease": -2,
    "easing": -2,
    "additional accommodation": -4,
    "support growth": -2,
    "downside risks": -2,
    "softening": -2,
    "subdued": -2,
    "weakening": -2,
    "cooling": -2,
    "disinflation": -3,
    "moderating": -1,
    "patient": -1,
    "appropriate to reduce": -3,
    "rate cuts": -3,
    "cut rates": -3,
    "begin to ease": -3,
}

# ── ECB — hawkish ↔ dovish ─────────────────────────────────────────────────
_ECB_POSITIVE = {
    "restrictive": 3,
    "sufficiently restrictive": 4,
    "elevated inflation": 3,
    "above target": 2,
    "core inflation": 2,
    "inflationary pressures": 3,
    "wage pressures": 2,
    "data-dependent": 1,
    "vigilant": 2,
    "second-round effects": 3,
    "underlying inflation": 2,
    "upside risks": 2,
    "transmission of monetary policy": 2,
}
_ECB_NEGATIVE = {
    "accommodative": -3,
    "ease": -2,
    "easing": -2,
    "downside risks": -2,
    "weakening": -2,
    "subdued": -2,
    "disinflation": -3,
    "moderating": -1,
    "rate cuts": -3,
    "cut rates": -3,
    "well anchored": -1,
    "moderation": -1,
    "softening": -2,
}

# ── MAS — pro-innovation ↔ pro-stability ───────────────────────────────────
_MAS_POSITIVE = {
    "fintech": 3,
    "innovation": 2,
    "sandbox": 3,
    "regulatory sandbox": 4,
    "digital banking": 3,
    "digital asset": 2,
    "experimentation": 2,
    "tokenisation": 3,
    "central bank digital currency": 3,
    "CBDC": 3,
    "fast-track": 2,
    "growth": 1,
    "global financial centre": 3,
}
_MAS_NEGATIVE = {
    "stability": 2,  # MAS uses positively but it's the conservative axis
    "financial stability": 3,
    "resilience": 2,
    "robust": 1,
    "vigilant": 2,
    "consumer protection": 2,
    "prudential": 2,
    "anti-money laundering": 2,
    "aml": 2,
    "scam": 2,
    "scams": 2,
    "vulnerable": 2,
}
# Note: MAS axis weights are inverted from the others — "stability" terms
# carry positive integer weight but live in _NEGATIVE so the score pushes
# toward the negative_label "PRO_STABILITY". The scorer's Σ(weight × count)
# does the right thing because we keep the dict-key sign convention: keys
# in _POSITIVE add up; keys in _NEGATIVE… also add up (their weights are
# positive ints here), but we flip the bucket-label semantics. Cleaner: use
# negative ints to keep the convention.
_MAS_NEGATIVE = {k: -abs(v) for k, v in _MAS_NEGATIVE.items()}


AXES: dict[str, StanceAxis] = {
    "SEC": StanceAxis(
        code="SEC",
        name="U.S. Securities and Exchange Commission",
        positive_label="PRO_ENFORCEMENT",
        negative_label="LIGHT_TOUCH",
        positive_long="Pro-enforcement (vigorous oversight, investor-protection emphasis)",
        negative_long="Light-touch (innovation-friendly, lower compliance burden)",
        positive=_SEC_POSITIVE,
        negative=_SEC_NEGATIVE,
    ),
    "FCA": StanceAxis(
        code="FCA",
        name="Financial Conduct Authority",
        positive_label="PRO_INNOVATION",
        negative_label="CONSUMER_FIRST",
        positive_long="Pro-innovation (growth & competitiveness secondary objective)",
        negative_long="Consumer-first (Consumer Duty, redress, vulnerable-consumer focus)",
        positive=_FCA_POSITIVE,
        negative=_FCA_NEGATIVE,
    ),
    "ESMA": StanceAxis(
        code="ESMA",
        name="European Securities and Markets Authority",
        positive_label="PRESCRIPTIVE",
        negative_label="PRINCIPLES_BASED",
        positive_long="Prescriptive (RTS, single-rulebook, harmonisation)",
        negative_long="Principles-based (proportionality, national discretion)",
        positive=_ESMA_POSITIVE,
        negative=_ESMA_NEGATIVE,
    ),
    "BoE": StanceAxis(
        code="BoE",
        name="Bank of England",
        positive_label="PRO_STABILITY",
        negative_label="PRO_GROWTH",
        positive_long="Pro-stability (macroprudential, capital buffers, systemic risk focus)",
        negative_long="Pro-growth (secondary objective, competitiveness, lower burden)",
        positive=_BOE_POSITIVE,
        negative=_BOE_NEGATIVE,
    ),
    "CFTC": StanceAxis(
        code="CFTC",
        name="Commodity Futures Trading Commission",
        positive_label="PRO_ENFORCEMENT",
        negative_label="LIGHT_TOUCH",
        positive_long="Pro-enforcement (manipulation, spoofing, civil penalties)",
        negative_long="Light-touch (regulatory clarity, no-action letters, sandbox)",
        positive=_CFTC_POSITIVE,
        negative=_CFTC_NEGATIVE,
    ),
    "Fed": StanceAxis(
        code="Fed",
        name="Federal Reserve",
        positive_label="HAWKISH",
        negative_label="DOVISH",
        positive_long="Hawkish (tighten/restrict, inflation-fighting language)",
        negative_long="Dovish (accommodative, ease, support-growth language)",
        positive=_FED_POSITIVE,
        negative=_FED_NEGATIVE,
    ),
    "ECB": StanceAxis(
        code="ECB",
        name="European Central Bank",
        positive_label="HAWKISH",
        negative_label="DOVISH",
        positive_long="Hawkish (restrictive, second-round effects, transmission)",
        negative_long="Dovish (accommodative, easing, disinflation language)",
        positive=_ECB_POSITIVE,
        negative=_ECB_NEGATIVE,
    ),
    "MAS": StanceAxis(
        code="MAS",
        name="Monetary Authority of Singapore",
        positive_label="PRO_INNOVATION",
        negative_label="PRO_STABILITY",
        positive_long="Pro-innovation (fintech, sandbox, tokenisation, CBDC)",
        negative_long="Pro-stability (resilience, prudential, AML, consumer protection)",
        positive=_MAS_POSITIVE,
        negative=_MAS_NEGATIVE,
    ),
}

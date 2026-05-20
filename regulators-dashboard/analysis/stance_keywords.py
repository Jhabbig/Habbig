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
}

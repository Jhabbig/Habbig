"""Beta(α, β) skill model + Wilson-score confidence interval.

We model each filer's directional accuracy as a Bernoulli process with
unknown success rate p, and put a uniform Beta(1, 1) prior on p. Each
labeled outcome (win/loss) updates the posterior:

    Beta(α, β) → Beta(α + win, β + 1 - win)

The posterior mean α / (α+β) is the "skill estimate". For confidence we
want a credible interval, but stdlib doesn't carry the inverse beta CDF.
The Wilson score interval is the well-known binomial CI that matches
this use case closely (and is more accurate than normal approximation
for small n). We use Wilson on the raw win count.

"High confidence skilled" = N ≥ MIN_N and lower CI bound > 0.55 (clearly
above coin flip).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PRIOR_ALPHA = 1.0
PRIOR_BETA  = 1.0

MIN_N_FOR_CONFIDENCE = 5
SKILL_CI_LOWER_THRESHOLD = 0.55


@dataclass
class SkillEstimate:
    wins: int
    losses: int
    n: int
    posterior_mean: float
    ci_lower: float
    ci_upper: float
    high_confidence_skilled: bool

    def as_dict(self) -> dict:
        return {
            "wins":           self.wins,
            "losses":         self.losses,
            "n":              self.n,
            "posterior_mean": round(self.posterior_mean, 4),
            "ci_lower":       round(self.ci_lower, 4),
            "ci_upper":       round(self.ci_upper, 4),
            "high_confidence_skilled": self.high_confidence_skilled,
        }


def estimate(wins: int, losses: int, z: float = 1.96) -> SkillEstimate:
    """Compute posterior mean + 95% Wilson-score CI."""
    n = wins + losses
    alpha = PRIOR_ALPHA + wins
    beta  = PRIOR_BETA  + losses
    posterior_mean = alpha / (alpha + beta)

    if n == 0:
        return SkillEstimate(
            wins=wins, losses=losses, n=0,
            posterior_mean=posterior_mean,
            ci_lower=0.0, ci_upper=1.0,
            high_confidence_skilled=False,
        )

    # Wilson score interval — uses the raw win count rather than the
    # posterior so the CI shrinks correctly as n grows.
    p = wins / n
    denom  = 1.0 + (z * z) / n
    center = (p + (z * z) / (2 * n)) / denom
    spread = (z * math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denom
    ci_lower = max(0.0, center - spread)
    ci_upper = min(1.0, center + spread)

    high_conf = (n >= MIN_N_FOR_CONFIDENCE) and (ci_lower > SKILL_CI_LOWER_THRESHOLD)
    return SkillEstimate(
        wins=wins, losses=losses, n=n,
        posterior_mean=posterior_mean,
        ci_lower=ci_lower, ci_upper=ci_upper,
        high_confidence_skilled=high_conf,
    )

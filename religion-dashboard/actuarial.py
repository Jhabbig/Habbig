"""Actuarial helpers for religious-leader survival modelling.

Inputs a leader's age + sex; outputs P(alive at age + N months) using a
US Social Security Administration period life table as the prior.

Why SSA: it's free, public, and broadly representative. Religious leaders
are not a random draw from this population — most have above-average
longevity (stable lifestyle, healthcare access, no manual labour). Use
SSA as a conservative prior; a Polymarket trader can adjust upward if
they have additional priors. The exposed P() is per-period, so it can
be combined with any caretaker-quality multiplier.

Anchor ages were taken from the SSA 2022 period life table (Office of
the Chief Actuary, public domain). q(x) = annual probability that an
x-year-old dies before reaching x+1.
"""

from __future__ import annotations

from datetime import date


LIFE_TABLE_MALE: dict[int, float] = {
    50: 0.00481, 55: 0.00733, 60: 0.01189, 65: 0.01788, 70: 0.02779,
    75: 0.04429, 80: 0.07186, 85: 0.11827, 86: 0.13030, 87: 0.14333,
    88: 0.15741, 89: 0.17260, 90: 0.18895, 91: 0.20652, 92: 0.22529,
    93: 0.24523, 94: 0.26625, 95: 0.28828, 96: 0.31114, 97: 0.33464,
    98: 0.35862, 99: 0.38291, 100: 0.40735, 105: 0.49961, 110: 0.58560,
}

LIFE_TABLE_FEMALE: dict[int, float] = {
    50: 0.00298, 55: 0.00457, 60: 0.00744, 65: 0.01166, 70: 0.01880,
    75: 0.03085, 80: 0.05206, 85: 0.09161, 90: 0.15875, 95: 0.25579,
    100: 0.37183, 105: 0.46578, 110: 0.55821,
}


def _interp_q(age: float, sex: str) -> float:
    """Linearly interpolate annual q(x) between life-table anchors."""
    table = LIFE_TABLE_FEMALE if (sex or "").upper().startswith("F") else LIFE_TABLE_MALE
    anchors = sorted(table.keys())
    if age <= anchors[0]:
        return table[anchors[0]]
    if age >= anchors[-1]:
        return table[anchors[-1]]
    for i, a in enumerate(anchors):
        if a >= age:
            lo, hi = anchors[i - 1], a
            t = (age - lo) / (hi - lo)
            return table[lo] * (1 - t) + table[hi] * t
    return table[anchors[-1]]


def survival_prob(age_years: float, sex: str, months_ahead: float) -> float:
    """P(person aged `age_years` (sex M/F) is alive `months_ahead` months from now).

    Walks forward in monthly steps, recomputing q at each step so the rate
    reflects the leader's age at that point in time.
    """
    if months_ahead <= 0:
        return 1.0
    p_alive = 1.0
    months_left = months_ahead
    cur = age_years
    while months_left > 0 and p_alive > 1e-9:
        annual_q = _interp_q(cur, sex)
        step = min(12.0, months_left)
        # P(survive step months) = (1 - annual_q)^(step/12)
        p_alive *= (1.0 - annual_q) ** (step / 12.0)
        cur += step / 12.0
        months_left -= step
    return p_alive


def age_on(born_iso: str, ref: date) -> float:
    """Fractional age in years on a reference date, given an ISO birth date."""
    y, m, d = (int(x) for x in born_iso.split("-"))
    delta_days = (ref - date(y, m, d)).days
    return delta_days / 365.2425

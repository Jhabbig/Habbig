"""Kelly-criterion position sizing helper.

For a binary YES market priced ``p_implied`` with model probability
``p_model`` and a bankroll ``B``, the Kelly fraction of bankroll to risk on
YES is:

    f* = (p_model - p_implied) / (1 - p_implied)

Negative ``f*`` means SELL YES (or BUY NO if the venue allows). We clamp to
[0, 0.25] - 25% bankroll on a single position is the *maximum* most
practitioners use even with full edge (full Kelly is too aggressive in
practice; half-Kelly to quarter-Kelly is standard).

Returns the suggested fraction plus a ``risk_dollars`` estimate for a
default ``$10000`` bankroll so the UI can show a concrete number.
"""
from __future__ import annotations

from typing import Optional


def kelly_fraction(p_model: Optional[float], p_implied: Optional[float]) -> Optional[float]:
    if p_model is None or p_implied is None:
        return None
    if p_implied <= 0 or p_implied >= 1:
        return None
    f = (p_model - p_implied) / (1.0 - p_implied)
    return max(-1.0, min(1.0, f))


def quarter_kelly(p_model: Optional[float], p_implied: Optional[float]) -> Optional[float]:
    """1/4 Kelly clamped to [-25%, +25%] - what most disciplined punters use."""
    f = kelly_fraction(p_model, p_implied)
    if f is None:
        return None
    return max(-0.25, min(0.25, f / 4.0))


def position_size(p_model: Optional[float], p_implied: Optional[float],
                   bankroll: float = 10000.0) -> Optional[dict]:
    f_full = kelly_fraction(p_model, p_implied)
    f_qk = quarter_kelly(p_model, p_implied)
    if f_full is None or f_qk is None:
        return None
    side = "YES" if f_full >= 0 else "NO"
    return {
        "side": side,
        "kelly_full": round(f_full, 3),
        "kelly_quarter": round(f_qk, 3),
        "suggested_bankroll_pct": round(abs(f_qk) * 100, 2),
        "suggested_dollars_at_10k": round(abs(f_qk) * bankroll, 2),
    }


if __name__ == "__main__":
    print("model=0.65 vs implied=0.50:", position_size(0.65, 0.50))
    print("model=0.30 vs implied=0.50:", position_size(0.30, 0.50))
    print("model=0.50 vs implied=0.50:", position_size(0.50, 0.50))
    print("model=0.95 vs implied=0.30:", position_size(0.95, 0.30))

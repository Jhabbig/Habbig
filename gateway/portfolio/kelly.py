"""Kelly criterion bet sizing for prediction markets.

The formula in this module is the "fractional-Kelly with cap" form
common in prediction-market tools:

  full_fraction = (p * b - q) / b          # classic Kelly
  where
    p = our estimated probability
    b = decimal odds minus 1 = (1 / market_price) - 1
    q = 1 - p

  recommended_fraction = min(full_fraction * kelly_fraction, max_cap)

We clip at ``max_cap`` (default 25%) because full Kelly is catastrophic
under any estimation error. Half and quarter Kelly are first-class in
the API so the frontend can surface three sizes side by side without
re-asking the server.

No network calls; no DB reads. Callers pass in the user's bankroll
explicitly (kept in ``users.bankroll_usd`` but fetched separately so
this module stays unit-testable).
"""

from __future__ import annotations

from typing import Optional


def kelly_fraction(
    our_prob: float,
    market_prob: float,
    *,
    max_cap: float = 0.25,
) -> float:
    """Return the full-Kelly fraction clipped at ``max_cap``.

    Returns 0 when our estimate is <= the market price (no edge) or
    when the inputs are degenerate (probs outside (0,1)). The cap
    keeps even a dramatic mispricing from blowing up a bankroll.
    """
    # Guard against nonsense inputs — callers may feed us floats out
    # of the conventional range.
    if not (0.0 < our_prob < 1.0):
        return 0.0
    if not (0.0 < market_prob < 1.0):
        return 0.0
    if our_prob <= market_prob:
        return 0.0
    b = (1.0 / market_prob) - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - our_prob
    raw = (our_prob * b - q) / b
    if raw <= 0:
        return 0.0
    return max(0.0, min(raw, max_cap))


def sizing_table(
    our_prob: float,
    market_prob: float,
    bankroll_usd: float,
    *,
    max_cap: float = 0.25,
) -> dict:
    """Return full / half / quarter Kelly sizes in USD plus P/L bounds.

    Used by both the ``/api/kelly/calculate`` route and the server-side
    dashboard renderer. Everything returned rounds to cents; the
    frontend doesn't need to format.
    """
    if bankroll_usd <= 0:
        return {
            "bankroll_usd": 0.0,
            "edge_pct": round(100.0 * (our_prob - market_prob), 2),
            "full_kelly_pct": 0.0,
            "full": _zero_size(),
            "half": _zero_size(),
            "quarter": _zero_size(),
            "note": "Set a bankroll in settings to get a recommendation",
        }
    full = kelly_fraction(our_prob, market_prob, max_cap=max_cap)
    half = full * 0.5
    quarter = full * 0.25

    # Max win per $1 at decimal odds b+1 is b per unit staked.
    # b = (1/market_prob) - 1, so stake * b is max profit; stake is max loss.
    b = ((1.0 / market_prob) - 1.0) if 0.0 < market_prob < 1.0 else 0.0

    def _row(frac: float) -> dict:
        stake = round(bankroll_usd * frac, 2)
        profit = round(stake * b, 2)
        return {
            "fraction": round(frac, 6),
            "stake_usd": stake,
            "max_profit_usd": profit,
            "max_loss_usd": stake,
        }

    return {
        "bankroll_usd": round(float(bankroll_usd), 2),
        "edge_pct": round(100.0 * (our_prob - market_prob), 2),
        "full_kelly_pct": round(100.0 * full, 2),
        "full": _row(full),
        "half": _row(half),
        "quarter": _row(quarter),
        "max_cap": max_cap,
    }


def _zero_size() -> dict:
    return {
        "fraction": 0.0,
        "stake_usd": 0.0,
        "max_profit_usd": 0.0,
        "max_loss_usd": 0.0,
    }


def get_user_bankroll(user_id: int) -> float:
    """Read ``users.bankroll_usd`` for a user. 0 means unset."""
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT bankroll_usd FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    if not row:
        return 0.0
    try:
        return float(row["bankroll_usd"] or 0)
    except (TypeError, ValueError, KeyError):
        return 0.0


def set_user_bankroll(user_id: int, bankroll_usd: float) -> None:
    import db
    with db.conn() as c:
        c.execute(
            "UPDATE users SET bankroll_usd = ? WHERE id = ?",
            (max(0.0, float(bankroll_usd)), user_id),
        )

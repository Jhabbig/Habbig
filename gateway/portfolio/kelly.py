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

Bankroll storage
----------------
The user's bankroll lives in ``users.bankroll`` (the column added by
migration 017 and read/written by ``queries/markets.py``,
``market_routes.py``, ``server.py`` settings/dashboard renderers, and
every existing test). This module used to read/write a parallel
``users.bankroll_usd`` column (migration 062), which silently diverged
from the canonical column — see ``audits/audit_kelly.md`` CRIT-1.

Migration 195 backfills any non-NULL ``bankroll_usd`` into ``bankroll``
(where bankroll IS NULL) and drops the ``bankroll_usd`` column via the
SQLite-rebuild dance. After that migration there is exactly one
bankroll column on ``users`` and exactly one read/write path.

No network calls; this module's DB helpers are tiny shims around the
canonical column so the calculator stays unit-testable.
"""

from __future__ import annotations

import math
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

    NaN / +/-Inf inputs (bankroll OR either probability) are coerced to
    the zero-bankroll response — they bypass ``<=`` comparisons in
    Python and would otherwise poison every numeric field downstream,
    producing literal ``NaN`` / ``Infinity`` tokens in the JSON
    response that browsers' ``JSON.parse`` rejects. See
    ``audits/audit_kelly.md`` HIGH-1.
    """
    # HIGH-1: filter non-finite floats before any arithmetic so NaN/Inf
    # can't propagate into stake / edge / max_profit. Treat any
    # non-finite input as "no bankroll" and surface the zero-bankroll
    # response — the safest fallback for a calculator UI.
    if (
        not math.isfinite(bankroll_usd)
        or not math.isfinite(our_prob)
        or not math.isfinite(market_prob)
        or bankroll_usd <= 0
    ):
        # Guard the edge_pct computation too: if either probability is
        # non-finite we can't compute a meaningful edge, so emit 0.0.
        if math.isfinite(our_prob) and math.isfinite(market_prob):
            edge_pct = round(100.0 * (our_prob - market_prob), 2)
        else:
            edge_pct = 0.0
        return {
            "bankroll_usd": 0.0,
            "edge_pct": edge_pct,
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
    """Read ``users.bankroll`` for a user. 0 means unset.

    Returns a plain ``float`` so callers stay schema-agnostic; for the
    dict shape (with ``kelly_fraction``) use ``db.get_user_bankroll``.
    Reads the canonical column — see migration 195 for the
    ``bankroll_usd`` -> ``bankroll`` consolidation.
    """
    import db
    with db.conn() as c:
        row = c.execute(
            "SELECT bankroll FROM users WHERE id = ?", (user_id,),
        ).fetchone()
    if not row:
        return 0.0
    try:
        value = float(row["bankroll"] or 0)
    except (TypeError, ValueError, IndexError, KeyError):
        return 0.0
    # Defence in depth: refuse to propagate non-finite floats out of
    # the data layer. A NULL or absent column would already be 0 via
    # the ``or 0`` above; this catches the impossible-but-not-yet-
    # impossible case where a stray NaN/Inf made it into the column
    # before migration 195 landed.
    if not math.isfinite(value):
        return 0.0
    return value


def set_user_bankroll(user_id: int, bankroll_usd: float) -> None:
    """Write ``users.bankroll`` for a user.

    Negatives are clamped to 0; NaN/Inf are rejected with ``ValueError``
    so a misbehaving caller can't poison the column. The route layer
    already validates the same shape (``portfolio/routes.py``), but
    this defence-in-depth keeps internal callers (cron, admin shell,
    backfills) honest. See ``audits/audit_kelly.md`` HIGH-3.

    Writes the canonical ``bankroll`` column — see migration 195.
    """
    import db
    v = float(bankroll_usd)
    if not math.isfinite(v):
        raise ValueError(f"bankroll must be finite, got {v!r}")
    v = max(0.0, v)
    with db.conn() as c:
        c.execute(
            "UPDATE users SET bankroll = ? WHERE id = ?",
            (v, user_id),
        )

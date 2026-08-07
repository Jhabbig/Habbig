"""Shared module-level constants — keep horizons consistent across modules."""

from __future__ import annotations

# Forward window for upcoming central-bank decisions and Kalshi/Polymarket
# market lookups. Three modules previously used 90 / 120 / 120 day variants;
# unify on 120 so the right-now headline doesn't claim "no meeting in 90 days"
# while implied-path is still showing a contract for one 100 days out.
HORIZON_DAYS = 120

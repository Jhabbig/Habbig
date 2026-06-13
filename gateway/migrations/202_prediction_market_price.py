"""Add ``market_price_at_prediction`` column to ``predictions``.

Why
---
The backtester (``gateway/backtest.py``: ``simulate`` /
``_load_predictions``) scores each prediction against the market price
*at the moment the prediction was made* — that's the price an edge would
have been taken at. The ``predictions`` table (see ``db.py`` schema, the
canonical CREATE TABLE) has no such column today; it only stores the
source's ``predicted_probability``, never the contemporaneous market
price. Without the as-of market price the simulator can't compute edge or
P&L, so backtest results would be meaningless.

This migration adds a single additive, nullable ``REAL`` column.
Existing rows stay valid with a NULL value (older predictions captured
before we recorded the as-of price simply won't be scorable and the
backtest can filter ``WHERE market_price_at_prediction IS NOT NULL``).

Idempotent — the column-add is guarded by a ``PRAGMA table_info`` check
and skipped if the column is already present, so re-running is safe.
"""

from __future__ import annotations

revision = "202"
down_revision = "201"


def upgrade(c):
    cols = {r["name"] for r in c.execute("PRAGMA table_info(predictions)").fetchall()}
    if "market_price_at_prediction" not in cols:
        c.execute(
            "ALTER TABLE predictions ADD COLUMN market_price_at_prediction REAL"
        )


def downgrade(c):
    # No-op: SQLite can only drop a column by rebuilding the table, and we
    # keep the column (and any captured as-of prices) even if a future
    # migration supersedes the schema. The column is additive and nullable
    # so leaving it in place is harmless.
    pass

"""Scenario tooling — conditional probability + correlation between markets.

Two user-facing tools, both Pro-gated:

  /tools/scenario       "if market X resolves YES, how do my other markets shift?"
  /tools/correlations   monochrome heatmap of top-30 active markets

Package layout:

  correlation.py   Pearson correlation over 90d of market_snapshots;
                   cached 1 day via cache.ttl_cache.
  scenario.py      Given (anchor, hypothetical), compute expected
                   probability shifts for every market with |r| > 0.25.

Both modules are pure functions + one sqlite3 reader each — no imports
from db.py, so they stay independently testable. Nothing here writes
to the DB except the saved-scenarios helper (which uses saved_views if
the tree has it, or a dedicated scenario_saves table otherwise).
"""

from scenarios.correlation import (  # noqa: F401
    compute_market_correlations,
    pearson,
    align_snapshot_series,
)
from scenarios.scenario import (  # noqa: F401
    compute_scenario,
    estimate_shift,
)

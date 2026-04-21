"""Credibility layer — calibration, timing, network analysis.

Pure-function modules: every entry point takes plain Python data (lists
of records, dicts, numbers) and returns plain Python data. No DB
connections are opened in the compute path — callers assemble the input
from whatever schema they have and pipe results back into whichever
table owns them.

Keeping this module data-plane-only lets the credibility math be unit
tested in isolation and reused by the backtester (which replays history
without touching the live DB).
"""

from credibility.calibration import (  # noqa: F401
    compute_brier_score,
    reliability_diagram_data,
    brier_component_for_record,
)
from credibility.timing import (  # noqa: F401
    compute_timing_score,
)
from credibility.network import (  # noqa: F401
    classify_relationship,
    pairwise_stats,
    echo_chamber_clusters,
    network_adjusted_consensus,
)

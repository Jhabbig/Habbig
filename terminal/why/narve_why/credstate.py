"""Source credibility state — the why engine's own context, never Julian's.

Two loaders: a JSON fixture (standalone/demo mode) and the terminal sidecar
sqlite DB (plain SQL against the ``sources`` table from
``terminal/sidecar/narve_sidecar/migrations/001_init.sql`` — no import of
narve_sidecar). ``n_resolved`` in DB mode is the count of ``credibility_events``
rows for the source: each event is one resolved-question scoring.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .schemas import ExplainRequest

__all__ = [
    "NEUTRAL_ALPHA",
    "NEUTRAL_BETA",
    "SourceState",
    "load_db_state",
    "load_fixture_state",
    "state_for",
]

NEUTRAL_ALPHA = 2.0
NEUTRAL_BETA = 2.0

_DB_QUERY = """
SELECT s.id, s.alpha, s.beta,
       (SELECT COUNT(*) FROM credibility_events e WHERE e.source_id = s.id)
FROM sources s
ORDER BY s.id
"""


@dataclass(frozen=True)
class SourceState:
    source_id: str
    alpha: float
    beta: float
    n_resolved: int

    @property
    def credibility(self) -> float:
        return self.alpha / (self.alpha + self.beta)


def load_fixture_state(path: Path) -> dict[str, SourceState]:
    """Load ``{source_id: {alpha, beta, n_resolved}}`` fixture JSON."""
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: fixture state must be a JSON object")
    states: dict[str, SourceState] = {}
    for source_id, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: entry for {source_id!r} must be an object")
        states[str(source_id)] = SourceState(
            source_id=str(source_id),
            alpha=float(entry["alpha"]),
            beta=float(entry["beta"]),
            n_resolved=int(entry["n_resolved"]),
        )
    return states


def load_db_state(db_path: Path) -> dict[str, SourceState]:
    """Read credibility state from the terminal sidecar sqlite DB (read-only)."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(_DB_QUERY).fetchall()
    finally:
        connection.close()
    states: dict[str, SourceState] = {}
    for source_id, alpha, beta, n_resolved in rows:
        states[str(source_id)] = SourceState(
            source_id=str(source_id),
            alpha=float(alpha),
            beta=float(beta),
            n_resolved=int(n_resolved),
        )
    return states


def state_for(
    request: ExplainRequest, state: dict[str, SourceState]
) -> dict[str, SourceState]:
    """States for the request's contributing sources; unknown ⇒ neutral prior."""
    result: dict[str, SourceState] = {}
    for output in request.model_outputs:
        if output.source_id in result:
            continue
        known = state.get(output.source_id)
        if known is None:
            known = SourceState(
                source_id=output.source_id,
                alpha=NEUTRAL_ALPHA,
                beta=NEUTRAL_BETA,
                n_resolved=0,
            )
        result[output.source_id] = known
    return result

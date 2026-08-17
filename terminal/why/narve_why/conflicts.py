"""Disagreement surfacing between credible sources.

A conflict is a pair of model outputs from credible sources (credibility
>= 0.6) whose stated probabilities sit >= 15 pts apart. Pairs from the same
source are skipped (a source cannot conflict with itself). Pair order and
note wording follow request order — fully deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .credstate import NEUTRAL_ALPHA, NEUTRAL_BETA, SourceState
from .schemas import ExplainRequest

__all__ = [
    "CONFLICT_THRESHOLD_PTS",
    "MIN_CREDIBILITY",
    "Conflict",
    "find_conflicts",
]

CONFLICT_THRESHOLD_PTS = 15.0
MIN_CREDIBILITY = 0.6


@dataclass(frozen=True)
class Conflict:
    note: str
    evidence_refs: tuple[str, ...]


def _credibility(states: dict[str, SourceState], source_id: str) -> float:
    state = states.get(source_id)
    if state is None:
        return NEUTRAL_ALPHA / (NEUTRAL_ALPHA + NEUTRAL_BETA)
    return state.credibility


def find_conflicts(
    req: ExplainRequest, states: dict[str, SourceState]
) -> list[Conflict]:
    """Pairwise conflicts between credible sources, in request order."""
    credible = [
        output
        for output in req.model_outputs
        if _credibility(states, output.source_id) >= MIN_CREDIBILITY
    ]
    conflicts: list[Conflict] = []
    for a, b in combinations(credible, 2):
        if a.source_id == b.source_id:
            continue
        gap = abs(a.p - b.p) * 100.0
        if gap < CONFLICT_THRESHOLD_PTS:
            continue
        note = (
            f"{a.source_id} ({a.p:.2f}) and {b.source_id} ({b.p:.2f})"
            f" are {gap:.0f} pts apart"
        )
        refs: list[str] = []
        for ref in a.inputs_ref + b.inputs_ref:
            if ref not in refs:
                refs.append(ref)
        conflicts.append(Conflict(note=note, evidence_refs=tuple(refs)))
    return conflicts

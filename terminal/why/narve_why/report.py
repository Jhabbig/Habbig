"""Assemble the ExplainReport — the only orchestrator in narve_why.

The probability arrives computed by the prediction side (CONTRACTS.md
Contract 1 v1.0.0); this module explains it and never recomputes it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from narve_why.conflicts import find_conflicts
from narve_why.credstate import SourceState, state_for
from narve_why.drivers import extract_drivers
from narve_why.prose import llm_polish, render_prose
from narve_why.schemas import ExplainRequest, MarketSnapshot
from narve_why.would_change import would_change


@dataclass(frozen=True)
class MarketGap:
    venue: str
    gap_pts: float
    read: str


@dataclass(frozen=True)
class DriverRow:
    tag: str
    label: str
    direction: str
    weight: float
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SourceRow:
    source_id: str
    credibility: float
    n_resolved: int
    stance_p: float
    contribution: float


@dataclass(frozen=True)
class ConflictRow:
    note: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ExplainReport:
    question_id: str
    probability: float
    market_gap: MarketGap | None
    drivers: tuple[DriverRow, ...]
    sources: tuple[SourceRow, ...]
    conflicts: tuple[ConflictRow, ...]
    would_change_it: tuple[str, ...]
    prose: str
    confidence_note: str


def _pick_snapshot(req: ExplainRequest) -> MarketSnapshot | None:
    """Deepest liquidity wins; liquidity tie -> latest capture (ISO-8601 sorts)."""
    if not req.market_snapshots:
        return None

    def key(snap: MarketSnapshot) -> tuple[float, str]:
        depth = snap.liquidity if snap.liquidity is not None else float("-inf")
        return (depth, snap.captured_at)

    return max(req.market_snapshots, key=key)


def _market_gap(probability: float, snap: MarketSnapshot | None) -> MarketGap | None:
    if snap is None:
        return None
    gap_pts = round((probability - snap.yes_price) * 100.0, 1)
    if gap_pts >= 2.0:
        read = "market looks cheap"
    elif gap_pts <= -2.0:
        read = "market looks rich"
    else:
        read = "in line with market"
    return MarketGap(venue=snap.venue, gap_pts=gap_pts, read=read)


_NEUTRAL_ALPHA = 2.0
_NEUTRAL_BETA = 2.0


def _request_states(
    req: ExplainRequest, states: dict[str, SourceState]
) -> dict[str, SourceState]:
    """State per contributing source only; unknown source -> neutral prior."""
    resolved = state_for(req, states)
    out: dict[str, SourceState] = {}
    for output in req.model_outputs:
        st = resolved.get(output.source_id)
        if st is None:
            st = SourceState(
                source_id=output.source_id,
                alpha=_NEUTRAL_ALPHA,
                beta=_NEUTRAL_BETA,
                n_resolved=0,
            )
        out[output.source_id] = st
    return out


def _source_rows(
    req: ExplainRequest, req_states: dict[str, SourceState]
) -> tuple[SourceRow, ...]:
    """One row per model output; contribution = normalised weight share (4dp)."""
    total_weight = sum(output.weight for output in req.model_outputs)
    count = len(req.model_outputs)
    rows: list[SourceRow] = []
    for output in req.model_outputs:
        share = output.weight / total_weight if total_weight > 0 else 1.0 / count
        st = req_states[output.source_id]
        rows.append(
            SourceRow(
                source_id=output.source_id,
                credibility=round(st.credibility, 4),
                n_resolved=st.n_resolved,
                stance_p=output.p,
                contribution=round(share, 4),
            )
        )
    rows.sort(key=lambda row: (-row.contribution, row.source_id))
    return tuple(rows)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _confidence_note(
    rows: tuple[SourceRow, ...], req_states: dict[str, SourceState]
) -> str:
    """Exact THIN / HIGH / MODERATE strings per INTERNALS.md."""
    n_sources = len(rows)
    total_resolved = sum(st.n_resolved for st in req_states.values())
    if n_sources < 2 or total_resolved == 0:
        return (
            f"THIN — {_plural(n_sources, 'source')},"
            f" {_plural(total_resolved, 'resolved call')} — context is thin"
        )
    stances = [row.stance_p for row in rows]
    spread_pts = (max(stances) - min(stances)) * 100.0
    high = [row for row in rows if row.credibility > 0.75]
    if len(high) >= 3 and spread_pts <= 5.0:
        return (
            f"HIGH — {len(high)} sources above 0.75 credibility,"
            f" agreeing within {spread_pts:.0f} pts"
        )
    return (
        f"MODERATE — {_plural(n_sources, 'source')}, spread {spread_pts:.0f} pts,"
        f" {_plural(total_resolved, 'resolved call')}"
    )


def explain(req: ExplainRequest, states: dict[str, SourceState]) -> ExplainReport:
    """Assemble the full ExplainReport. Deterministic; never recomputes p."""
    req_states = _request_states(req, states)
    raw_drivers = extract_drivers(req)
    driver_rows = tuple(
        DriverRow(
            tag=d.tag,
            label=d.label,
            direction=d.direction,
            weight=round(float(d.weight), 4),
            evidence_refs=tuple(d.evidence_refs),
        )
        for d in raw_drivers
    )
    conflict_rows = tuple(
        ConflictRow(note=c.note, evidence_refs=tuple(c.evidence_refs))
        for c in find_conflicts(req, req_states)
    )
    watch = tuple(would_change(req, raw_drivers))
    snap = _pick_snapshot(req)
    gap = _market_gap(req.probability, snap)
    source_rows = _source_rows(req, req_states)
    confidence_note = _confidence_note(source_rows, req_states)
    parts: dict[str, Any] = {
        "probability": req.probability,
        "market": None
        if snap is None or gap is None
        else {"venue": snap.venue, "yes_price": snap.yes_price, "read": gap.read},
        "drivers": [
            {"label": d.label, "direction": d.direction, "weight": d.weight}
            for d in driver_rows
        ],
        "conflicts": [{"note": c.note} for c in conflict_rows],
        "would_change_it": list(watch),
        "confidence_note": confidence_note,
    }
    prose = llm_polish(render_prose(parts), parts)
    return ExplainReport(
        question_id=req.question_id,
        probability=req.probability,
        market_gap=gap,
        drivers=driver_rows,
        sources=source_rows,
        conflicts=conflict_rows,
        would_change_it=watch,
        prose=prose,
        confidence_note=confidence_note,
    )


def to_json(report: ExplainReport) -> str:
    """THE byte-stable serialisation: sorted keys, 2-space indent, trailing \\n."""
    payload: dict[str, Any] = {
        "question_id": report.question_id,
        "probability": report.probability,
        "market_gap": None
        if report.market_gap is None
        else {
            "venue": report.market_gap.venue,
            "gap_pts": report.market_gap.gap_pts,
            "read": report.market_gap.read,
        },
        "drivers": [
            {
                "tag": d.tag,
                "label": d.label,
                "direction": d.direction,
                "weight": d.weight,
                "evidence_refs": list(d.evidence_refs),
            }
            for d in report.drivers
        ],
        "sources": [
            {
                "source_id": s.source_id,
                "credibility": s.credibility,
                "n_resolved": s.n_resolved,
                "stance_p": s.stance_p,
                "contribution": s.contribution,
            }
            for s in report.sources
        ],
        "conflicts": [
            {"note": c.note, "evidence_refs": list(c.evidence_refs)}
            for c in report.conflicts
        ],
        "would_change_it": list(report.would_change_it),
        "prose": report.prose,
        "confidence_note": report.confidence_note,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

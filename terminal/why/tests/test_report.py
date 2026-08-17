"""Pins for report assembly: market_gap venue choice, contribution sums,
confidence_note strings, to_json byte-stability. Requests are built
inline — no fixtures/ dependency."""
from __future__ import annotations

import json

from narve_why.credstate import SourceState
from narve_why.report import explain, to_json
from narve_why.schemas import ExplainRequest, MarketSnapshot, ModelOutput


def make_output(source_id: str, p: float, weight: float) -> ModelOutput:
    return ModelOutput(
        model_id=source_id,
        source_id=source_id,
        p=p,
        weight=weight,
        inputs_ref=(f"pred:{source_id}",),
    )


def make_request(
    probability: float = 0.62,
    outputs: tuple[ModelOutput, ...] | None = None,
    snapshots: tuple[MarketSnapshot, ...] = (),
) -> ExplainRequest:
    if outputs is None:
        outputs = (
            make_output("race_model", 0.64, 1.0),
            make_output("state_polls", 0.61, 1.0),
            make_output("macro_model", 0.34, 2.0),
        )
    return ExplainRequest(
        question_id="q-test",
        question_text="Does the test question resolve yes?",
        probability=probability,
        as_of="2026-07-01T09:00:00Z",
        model_outputs=outputs,
        market_snapshots=snapshots,
    )


def make_states(
    n_resolved: int = 10, alpha: float = 16.0, beta: float = 4.0
) -> dict[str, SourceState]:
    ids = ("race_model", "state_polls", "macro_model")
    return {
        sid: SourceState(source_id=sid, alpha=alpha, beta=beta, n_resolved=n_resolved)
        for sid in ids
    }


def test_market_gap_picks_deepest_liquidity() -> None:
    snaps = (
        MarketSnapshot(venue="kalshi", yes_price=0.55, liquidity=120000.0,
                       captured_at="2026-07-01T08:40:00Z"),
        MarketSnapshot(venue="polymarket", yes_price=0.50, liquidity=250000.0,
                       captured_at="2026-07-01T08:10:00Z"),
    )
    report = explain(make_request(snapshots=snaps), make_states())
    assert report.market_gap is not None
    assert report.market_gap.venue == "polymarket"
    assert report.market_gap.gap_pts == 12.0
    assert report.market_gap.read == "market looks cheap"


def test_market_gap_liquidity_tie_takes_latest_capture() -> None:
    snaps = (
        MarketSnapshot(venue="kalshi", yes_price=0.55, liquidity=100000.0,
                       captured_at="2026-07-01T08:40:00Z"),
        MarketSnapshot(venue="polymarket", yes_price=0.61, liquidity=100000.0,
                       captured_at="2026-07-01T08:55:00Z"),
    )
    report = explain(make_request(snapshots=snaps), make_states())
    assert report.market_gap is not None
    assert report.market_gap.venue == "polymarket"
    assert report.market_gap.gap_pts == 1.0
    assert report.market_gap.read == "in line with market"


def test_market_gap_rich_read() -> None:
    snaps = (
        MarketSnapshot(venue="kalshi", yes_price=0.70, liquidity=1.0,
                       captured_at="2026-07-01T08:40:00Z"),
    )
    report = explain(make_request(snapshots=snaps), make_states())
    assert report.market_gap is not None
    assert report.market_gap.gap_pts == -8.0
    assert report.market_gap.read == "market looks rich"


def test_market_gap_null_without_snapshots() -> None:
    report = explain(make_request(), make_states())
    assert report.market_gap is None
    assert "no market to compare" in report.prose


def test_contributions_are_normalised_weight_shares() -> None:
    report = explain(make_request(), make_states())
    by_id = {row.source_id: row for row in report.sources}
    assert by_id["macro_model"].contribution == 0.5
    assert by_id["race_model"].contribution == 0.25
    assert by_id["state_polls"].contribution == 0.25
    assert abs(sum(row.contribution for row in report.sources) - 1.0) < 1e-6
    contributions = [row.contribution for row in report.sources]
    assert contributions == sorted(contributions, reverse=True)


def test_confidence_thin_single_source() -> None:
    req = make_request(outputs=(make_output("lone_model", 0.6, 1.0),))
    states = {
        "lone_model": SourceState(
            source_id="lone_model", alpha=2.0, beta=2.0, n_resolved=0
        )
    }
    report = explain(req, states)
    assert report.confidence_note == (
        "THIN — 1 source, 0 resolved calls — context is thin"
    )


def test_confidence_thin_zero_resolved() -> None:
    report = explain(make_request(), make_states(n_resolved=0))
    assert report.confidence_note == (
        "THIN — 3 sources, 0 resolved calls — context is thin"
    )


def test_confidence_high() -> None:
    outputs = (
        make_output("race_model", 0.63, 1.0),
        make_output("state_polls", 0.61, 1.0),
        make_output("macro_model", 0.60, 1.0),
    )
    # alpha=16, beta=4 -> credibility 0.8 > 0.75; spread 3 pts <= 5
    report = explain(make_request(outputs=outputs), make_states(n_resolved=10))
    assert report.confidence_note == (
        "HIGH — 3 sources above 0.75 credibility, agreeing within 3 pts"
    )


def test_confidence_moderate() -> None:
    # spread 0.64-0.34 = 30 pts breaks the HIGH agreement bound
    report = explain(make_request(), make_states(n_resolved=4))
    assert report.confidence_note == (
        "MODERATE — 3 sources, spread 30 pts, 12 resolved calls"
    )


def test_to_json_byte_stable() -> None:
    snaps = (
        MarketSnapshot(venue="kalshi", yes_price=0.55, liquidity=120000.0,
                       captured_at="2026-07-01T08:40:00Z"),
    )
    first = to_json(explain(make_request(snapshots=snaps), make_states()))
    second = to_json(explain(make_request(snapshots=snaps), make_states()))
    assert first.encode("utf-8") == second.encode("utf-8")
    assert first.endswith("\n")
    payload = json.loads(first)
    assert list(payload) == sorted(payload)  # sorted keys
    assert first.splitlines()[1].startswith('  "')  # 2-space indent
    assert payload["probability"] == 0.62
    assert payload["market_gap"] == {
        "venue": "kalshi", "gap_pts": 7.0, "read": "market looks cheap"
    }

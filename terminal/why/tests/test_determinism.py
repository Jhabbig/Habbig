"""Acceptance layer: byte-determinism + evidence traceability over EVERY fixture.

Standalone-safe by design: the engine modules (narve_why.*) may not exist yet
when this file lands — every engine-touching test uses pytest.importorskip and
skips cleanly. The raw-JSON fixture checks always run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WHY_ROOT = Path(__file__).resolve().parents[1]
if str(WHY_ROOT) not in sys.path:
    sys.path.insert(0, str(WHY_ROOT))

FIXTURES = WHY_ROOT / "fixtures"
STATE_PATH = FIXTURES / "sources_state.json"

# Every fixture is an ExplainRequest except the credibility-state file.
REQUEST_FIXTURES = sorted(
    p for p in FIXTURES.glob("*.json") if p.name != "sources_state.json"
)
TAG_FIXTURES = [p for p in REQUEST_FIXTURES if p.name.startswith("tag_")]
_IDS = [p.name for p in REQUEST_FIXTURES]
_TAG_IDS = [p.name for p in TAG_FIXTURES]


def _explain_json(path: Path) -> str:
    """Parse fixture -> fixture credibility state -> explain -> to_json str."""
    schemas = pytest.importorskip("narve_why.schemas")
    credstate = pytest.importorskip("narve_why.credstate")
    report_mod = pytest.importorskip("narve_why.report")
    req = schemas.parse_request(json.loads(path.read_text(encoding="utf-8")))
    full_state = credstate.load_fixture_state(STATE_PATH)
    states = credstate.state_for(req, full_state)
    return report_mod.to_json(report_mod.explain(req, states))


# ---------- always runnable (no engine import needed) ----------------------

def test_fixture_set_is_complete() -> None:
    expected = {"house_majority.json", "hard_conflict.json", "thin_data.json"} | {
        f"tag_{t}.json"
        for t in ("governance", "funding", "polling", "economy",
                  "legal", "incident", "momentum", "market_structure")
    }
    assert {p.name for p in REQUEST_FIXTURES} >= expected


def test_sources_state_matches_terminal_sample_db() -> None:
    # Post-load_sample numbers from terminal/sidecar/narve_sidecar/sample_data.py
    # (seeds 12/3 and 19/5 plus the two pre-resolved 2025 governor questions).
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    expected = {
        "race_model": (13.0, 3.0, 1),
        "poll_aggregator": (19.0, 6.0, 1),
        "state_polls": (12.0, 5.0, 0),
        "macro_model": (10.0, 3.0, 0),
        "generic_ballot": (20.0, 7.0, 0),
    }
    assert set(state) == set(expected)
    for sid, (alpha, beta, n) in expected.items():
        row = state[sid]
        assert (row["alpha"], row["beta"], row["n_resolved"]) == (alpha, beta, n), sid


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_IDS)
def test_fixture_json_is_valid_request_shape(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    for key in ("question_id", "question_text", "probability", "as_of",
                "model_outputs", "market_snapshots"):
        assert key in raw, f"{path.name}: missing {key}"
    assert 0.0 <= raw["probability"] <= 1.0, f"{path.name}: probability"
    assert raw["model_outputs"], f"{path.name}: model_outputs must be non-empty"
    for i, mo in enumerate(raw["model_outputs"]):
        assert mo.get("inputs_ref"), (
            f"{path.name}: model_outputs[{i}].inputs_ref empty — contract violation"
        )
        assert 0.0 <= mo["p"] <= 1.0, f"{path.name}: model_outputs[{i}].p"
        assert mo["weight"] >= 0.0, f"{path.name}: model_outputs[{i}].weight"
    for i, snap in enumerate(raw["market_snapshots"]):
        assert 0.0 <= snap["yes_price"] <= 1.0, (
            f"{path.name}: market_snapshots[{i}].yes_price"
        )


# ---------- engine-dependent (skip until siblings land) --------------------

@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_IDS)
def test_fixture_parses(path: Path) -> None:
    schemas = pytest.importorskip("narve_why.schemas")
    req = schemas.parse_request(json.loads(path.read_text(encoding="utf-8")))
    assert req.model_outputs, path.name


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_IDS)
def test_explain_twice_is_byte_identical(path: Path) -> None:
    first = _explain_json(path).encode("utf-8")
    second = _explain_json(path).encode("utf-8")
    assert first == second, f"{path.name}: explain() is not byte-deterministic"


@pytest.mark.parametrize("path", REQUEST_FIXTURES, ids=_IDS)
def test_every_entity_carries_evidence(path: Path) -> None:
    """Traceability invariant: empty evidence_refs anywhere = FAIL.

    Drivers and conflicts must carry the key non-empty (their normative JSON
    shape includes it). The normative source-row shape in CONTRACTS.md does
    not include evidence_refs, so for sources the key is optional — but if
    present it must be non-empty.
    """
    report = json.loads(_explain_json(path))
    for i, d in enumerate(report["drivers"]):
        assert d.get("evidence_refs"), f"{path.name}: drivers[{i}] has no evidence"
    for i, c in enumerate(report["conflicts"]):
        assert c.get("evidence_refs"), f"{path.name}: conflicts[{i}] has no evidence"
    for i, s in enumerate(report["sources"]):
        if "evidence_refs" in s:
            assert s["evidence_refs"], f"{path.name}: sources[{i}] evidence empty"


@pytest.mark.parametrize("path", TAG_FIXTURES, ids=_TAG_IDS)
def test_tag_fixture_pins_top_driver_tag(path: Path) -> None:
    """tag_<x>.json must surface <x> as the TOP driver — pins the vocabulary."""
    tag = path.stem.removeprefix("tag_")
    report = json.loads(_explain_json(path))
    assert report["drivers"], f"{path.name}: no drivers extracted"
    top = report["drivers"][0]
    assert top["tag"] == tag, (
        f"{path.name}: top driver tag {top['tag']!r}, expected {tag!r}"
    )
    assert top["direction"] in ("up", "down"), path.name


def test_thin_data_forces_thin_confidence_note() -> None:
    report = json.loads(_explain_json(FIXTURES / "thin_data.json"))
    assert report["confidence_note"].startswith("THIN"), report["confidence_note"]


def test_hard_conflict_surfaces_a_conflict() -> None:
    report = json.loads(_explain_json(FIXTURES / "hard_conflict.json"))
    assert report["conflicts"], "two credible sources 38 pts apart must conflict"

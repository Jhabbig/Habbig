"""Conflict threshold boundaries, note format, and credibility-state loaders."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from narve_why.conflicts import CONFLICT_THRESHOLD_PTS, find_conflicts
from narve_why.credstate import (
    SourceState,
    load_db_state,
    load_fixture_state,
    state_for,
)
from narve_why.schemas import ExplainRequest, ModelOutput


def _output(source_id: str, p: float, refs: tuple[str, ...]) -> ModelOutput:
    return ModelOutput(
        model_id=source_id, source_id=source_id, p=p, weight=0.5, inputs_ref=refs
    )


def _request(outputs: tuple[ModelOutput, ...]) -> ExplainRequest:
    return ExplainRequest(
        question_id="q-1",
        question_text="Do Republicans win the U.S. House majority?",
        probability=0.62,
        as_of="2026-07-01T09:00:00Z",
        model_outputs=outputs,
        market_snapshots=(),
    )


def _credible(source_id: str) -> SourceState:
    return SourceState(source_id=source_id, alpha=8.0, beta=2.0, n_resolved=4)  # 0.8


def _weak(source_id: str) -> SourceState:
    return SourceState(source_id=source_id, alpha=2.0, beta=2.0, n_resolved=0)  # 0.5


def test_threshold_constant() -> None:
    assert CONFLICT_THRESHOLD_PTS == 15.0


def test_gap_14_9_is_not_a_conflict() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("beta_src", 0.471, ("ref:b",)))
    )
    states = {"alpha_src": _credible("alpha_src"), "beta_src": _credible("beta_src")}
    assert find_conflicts(req, states) == []


def test_gap_15_1_is_a_conflict() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("beta_src", 0.469, ("ref:b",)))
    )
    states = {"alpha_src": _credible("alpha_src"), "beta_src": _credible("beta_src")}
    conflicts = find_conflicts(req, states)
    assert len(conflicts) == 1
    assert conflicts[0].note == "alpha_src (0.62) and beta_src (0.47) are 15 pts apart"
    assert conflicts[0].evidence_refs == ("ref:a", "ref:b")


def test_gap_exactly_15_is_a_conflict() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("beta_src", 0.47, ("ref:b",)))
    )
    states = {"alpha_src": _credible("alpha_src"), "beta_src": _credible("beta_src")}
    assert len(find_conflicts(req, states)) == 1


def test_low_cred_pair_is_ignored() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("beta_src", 0.30, ("ref:b",)))
    )
    states = {"alpha_src": _credible("alpha_src"), "beta_src": _weak("beta_src")}
    assert find_conflicts(req, states) == []


def test_credibility_exactly_0_6_counts() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("edge_src", 0.30, ("ref:b",)))
    )
    states = {
        "alpha_src": _credible("alpha_src"),
        "edge_src": SourceState("edge_src", alpha=3.0, beta=2.0, n_resolved=1),  # 0.6
    }
    assert len(find_conflicts(req, states)) == 1


def test_unknown_source_gets_neutral_prior_and_is_excluded() -> None:
    req = _request(
        (_output("alpha_src", 0.62, ("ref:a",)), _output("stranger", 0.30, ("ref:b",)))
    )
    states = {"alpha_src": _credible("alpha_src")}  # stranger absent -> 0.5 cred
    assert find_conflicts(req, states) == []


def test_same_source_pair_is_skipped() -> None:
    req = _request(
        (_output("alpha_src", 0.80, ("ref:a1",)), _output("alpha_src", 0.40, ("ref:a2",)))
    )
    states = {"alpha_src": _credible("alpha_src")}
    assert find_conflicts(req, states) == []


def test_note_matches_contract_sample_format() -> None:
    req = _request(
        (
            _output("state_polls", 0.61, ("poll:mi-0609",)),
            _output("macro_model", 0.34, ("pred:9",)),
        )
    )
    states = {"state_polls": _credible("state_polls"), "macro_model": _credible("macro_model")}
    conflicts = find_conflicts(req, states)
    assert len(conflicts) == 1
    assert conflicts[0].note == "state_polls (0.61) and macro_model (0.34) are 27 pts apart"
    assert conflicts[0].evidence_refs == ("poll:mi-0609", "pred:9")


def test_credibility_property() -> None:
    assert SourceState("race_model", alpha=13.0, beta=3.0, n_resolved=1).credibility == 0.8125
    assert _weak("x").credibility == 0.5


def test_state_for_neutral_prior_fallback() -> None:
    req = _request((_output("alpha_src", 0.6, ("r:1",)), _output("stranger", 0.5, ("r:2",))))
    states = state_for(req, {"alpha_src": _credible("alpha_src")})
    assert set(states) == {"alpha_src", "stranger"}
    assert states["stranger"] == SourceState("stranger", alpha=2.0, beta=2.0, n_resolved=0)
    assert states["alpha_src"].credibility == 0.8


def test_load_fixture_state_roundtrip(tmp_path: Path) -> None:
    fixture = tmp_path / "sources_state.json"
    fixture.write_text(
        json.dumps({"race_model": {"alpha": 13.0, "beta": 3.0, "n_resolved": 1}}),
        encoding="utf-8",
    )
    states = load_fixture_state(fixture)
    assert states == {"race_model": SourceState("race_model", 13.0, 3.0, 1)}
    assert states["race_model"].credibility == 0.8125


def test_load_db_state_reads_sidecar_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sources(
            id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
            alpha REAL NOT NULL DEFAULT 2.0, beta REAL NOT NULL DEFAULT 2.0,
            is_sample INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE credibility_events(
            id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, question_id TEXT NOT NULL,
            old_alpha REAL, old_beta REAL, new_alpha REAL, new_beta REAL, at TEXT NOT NULL);
        INSERT INTO sources(id, name, alpha, beta, created_at)
            VALUES ('race_model', 'Race model', 13.0, 3.0, '2026-07-01T00:00:00Z');
        INSERT INTO sources(id, name, alpha, beta, created_at)
            VALUES ('state_polls', 'State polls', 12.0, 5.0, '2026-07-01T00:00:00Z');
        INSERT INTO credibility_events(source_id, question_id, at)
            VALUES ('race_model', 'q-1', '2026-07-02T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()
    states = load_db_state(db_path)
    assert states["race_model"] == SourceState("race_model", 13.0, 3.0, 1)
    assert states["state_polls"] == SourceState("state_polls", 12.0, 5.0, 0)
    assert states["race_model"].credibility == 0.8125

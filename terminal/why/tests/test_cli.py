"""CLI pins: json/text formats and ContractError -> exit 2 with the exact
field path on stderr. Runs the CLI via subprocess on inline temp payloads."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

PAYLOAD: dict[str, Any] = {
    "question_id": "cli-test",
    "question_text": "Does the CLI hold up?",
    "probability": 0.62,
    "as_of": "2026-07-01T09:00:00Z",
    "model_outputs": [
        {"model_id": "race_model", "source_id": "race_model", "p": 0.64,
         "weight": 0.5, "inputs_ref": ["pred:1"]},
        {"model_id": "state_polls", "source_id": "state_polls", "p": 0.61,
         "weight": 0.5, "inputs_ref": ["poll:mi-0609"]},
    ],
    "market_snapshots": [
        {"venue": "kalshi", "yes_price": 0.55, "liquidity": 120000,
         "captured_at": "2026-07-01T08:40:00Z"}
    ],
}

STATE: dict[str, Any] = {
    "race_model": {"alpha": 15.0, "beta": 5.0, "n_resolved": 16},
    "state_polls": {"alpha": 14.0, "beta": 7.0, "n_resolved": 17},
}


def run_cli(*args: str) -> "subprocess.CompletedProcess[str]":
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "narve_why", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
    )


def write_inputs(tmp_path: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    payload_path = tmp_path / "payload.json"
    state_path = tmp_path / "state.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    state_path.write_text(json.dumps(STATE), encoding="utf-8")
    return payload_path, state_path


def test_json_format(tmp_path: Path) -> None:
    payload_path, state_path = write_inputs(tmp_path, PAYLOAD)
    proc = run_cli("explain", "--in", str(payload_path),
                   "--state", str(state_path), "--format", "json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["question_id"] == "cli-test"
    assert report["probability"] == 0.62
    assert report["market_gap"]["venue"] == "kalshi"
    assert report["market_gap"]["gap_pts"] == 7.0
    assert proc.stdout.endswith("\n")


def test_json_is_default_and_deterministic(tmp_path: Path) -> None:
    payload_path, state_path = write_inputs(tmp_path, PAYLOAD)
    first = run_cli("explain", "--in", str(payload_path), "--state", str(state_path))
    second = run_cli("explain", "--in", str(payload_path), "--state", str(state_path))
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["question_id"] == "cli-test"


def test_text_format_card(tmp_path: Path) -> None:
    payload_path, state_path = write_inputs(tmp_path, PAYLOAD)
    proc = run_cli("explain", "--in", str(payload_path),
                   "--state", str(state_path), "--format", "text")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    # probability + market gap on line 1
    assert "62%" in lines[0]
    assert "kalshi" in lines[0]
    assert "market looks cheap" in lines[0]
    # the card sections
    assert any("TOP DRIVERS" in line for line in lines)
    assert any("DISSENT" in line for line in lines)
    assert any("WATCH" in line for line in lines)
    assert any("CONFIDENCE" in line for line in lines)
    # direction arrows with weights
    assert any(("↑" in line) or ("↓" in line) for line in lines)
    # ten-second card: fits one screenful
    assert len(lines) <= 25


def test_contract_error_exits_2_with_field_path(tmp_path: Path) -> None:
    bad: dict[str, Any] = json.loads(json.dumps(PAYLOAD))
    bad["model_outputs"][0]["p"] = 2.0
    payload_path, state_path = write_inputs(tmp_path, bad)
    proc = run_cli("explain", "--in", str(payload_path), "--state", str(state_path))
    assert proc.returncode == 2
    assert "model_outputs[0].p" in proc.stderr
    assert proc.stdout == ""


def test_invalid_json_payload_exits_2(tmp_path: Path) -> None:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{not json", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(STATE), encoding="utf-8")
    proc = run_cli("explain", "--in", str(payload_path), "--state", str(state_path))
    assert proc.returncode == 2
    assert "payload" in proc.stderr

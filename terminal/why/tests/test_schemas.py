"""Contract validation: exact error strings are the product — assert them byte-for-byte."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from narve_why.schemas import ContractError, parse_request

_BASE: dict[str, Any] = {
    "question_id": "midterm-house-gop-2026",
    "question_text": "Do Republicans win the U.S. House majority?",
    "probability": 0.62,
    "as_of": "2026-07-01T09:00:00Z",
    "model_outputs": [
        {"model_id": "race_model", "source_id": "race_model", "p": 0.64,
         "weight": 0.34, "inputs_ref": ["pred:1", "poll:az-0612"]},
        {"model_id": "state_polls", "source_id": "state_polls", "p": 0.61,
         "weight": 0.31, "inputs_ref": ["poll:mi-0609"]},
        {"model_id": "macro_model", "source_id": "macro_model", "p": 0.47,
         "weight": 0.35, "inputs_ref": ["pred:9"]},
    ],
    "market_snapshots": [
        {"venue": "kalshi", "yes_price": 0.55, "liquidity": 120000,
         "captured_at": "2026-07-01T08:40:00Z"},
    ],
}


def payload() -> dict[str, Any]:
    return copy.deepcopy(_BASE)


def raises_exactly(raw: dict[str, Any], message: str, field_path: str) -> None:
    with pytest.raises(ContractError) as excinfo:
        parse_request(raw)
    assert str(excinfo.value) == message
    assert excinfo.value.field_path == field_path


def test_happy_path_parses() -> None:
    req = parse_request(payload())
    assert req.question_id == "midterm-house-gop-2026"
    assert req.probability == 0.62
    assert req.as_of == "2026-07-01T09:00:00Z"
    assert len(req.model_outputs) == 3
    assert req.model_outputs[0].inputs_ref == ("pred:1", "poll:az-0612")
    assert req.model_outputs[2].weight == 0.35
    assert req.market_snapshots[0].venue == "kalshi"
    assert req.market_snapshots[0].liquidity == 120000.0


def test_unknown_fields_ignored() -> None:
    raw = payload()
    raw["shiny_new_field"] = {"nested": True}
    raw["model_outputs"][0]["extra"] = "ignored"
    raw["market_snapshots"][0]["depth_chart"] = [1, 2, 3]
    req = parse_request(raw)
    assert not hasattr(req, "shiny_new_field")
    assert len(req.model_outputs) == 3


def test_p_out_of_range_exact_message() -> None:
    raw = payload()
    raw["model_outputs"][2]["p"] = 1.4
    raises_exactly(
        raw,
        "model_outputs[2].p: must be within [0, 1] (got 1.4)",
        "model_outputs[2].p",
    )


def test_probability_out_of_range() -> None:
    raw = payload()
    raw["probability"] = -0.2
    raises_exactly(
        raw,
        "probability: must be within [0, 1] (got -0.2)",
        "probability",
    )


def test_yes_price_out_of_range() -> None:
    raw = payload()
    raw["market_snapshots"][0]["yes_price"] = 1.5
    raises_exactly(
        raw,
        "market_snapshots[0].yes_price: must be within [0, 1] (got 1.5)",
        "market_snapshots[0].yes_price",
    )


def test_empty_model_outputs_refused() -> None:
    raw = payload()
    raw["model_outputs"] = []
    raises_exactly(raw, "model_outputs: must be non-empty (got [])", "model_outputs")


def test_empty_inputs_ref_refused() -> None:
    raw = payload()
    raw["model_outputs"][0]["inputs_ref"] = []
    raises_exactly(
        raw,
        "model_outputs[0].inputs_ref: must be non-empty (got [])",
        "model_outputs[0].inputs_ref",
    )


def test_negative_weight_refused() -> None:
    raw = payload()
    raw["model_outputs"][1]["weight"] = -0.5
    raises_exactly(
        raw,
        "model_outputs[1].weight: must be >= 0 (got -0.5)",
        "model_outputs[1].weight",
    )


def test_unparseable_timestamp_refused() -> None:
    raw = payload()
    raw["as_of"] = "not-a-date"
    raises_exactly(
        raw,
        "as_of: must be an ISO-8601 UTC timestamp (got 'not-a-date')",
        "as_of",
    )


def test_naive_timestamp_refused() -> None:
    raw = payload()
    raw["as_of"] = "2026-07-01T09:00:00"
    raises_exactly(
        raw,
        "as_of: must be an ISO-8601 UTC timestamp (got '2026-07-01T09:00:00')",
        "as_of",
    )


def test_non_string_captured_at_refused() -> None:
    raw = payload()
    raw["market_snapshots"][0]["captured_at"] = 5
    raises_exactly(
        raw,
        "market_snapshots[0].captured_at: must be an ISO-8601 UTC timestamp (got 5)",
        "market_snapshots[0].captured_at",
    )


def test_explicit_utc_offset_accepted() -> None:
    raw = payload()
    raw["as_of"] = "2026-07-01T09:00:00+00:00"
    assert parse_request(raw).as_of == "2026-07-01T09:00:00+00:00"


def test_missing_required_field() -> None:
    raw = payload()
    del raw["question_text"]
    raises_exactly(
        raw,
        "question_text: missing required field (got None)",
        "question_text",
    )


def test_missing_market_snapshots_defaults_empty() -> None:
    raw = payload()
    del raw["market_snapshots"]
    assert parse_request(raw).market_snapshots == ()


def test_bool_not_accepted_as_number() -> None:
    raw = payload()
    raw["model_outputs"][0]["p"] = True
    raises_exactly(
        raw,
        "model_outputs[0].p: must be a number (got True)",
        "model_outputs[0].p",
    )


def test_request_is_frozen() -> None:
    req = parse_request(payload())
    with pytest.raises(AttributeError):
        req.probability = 0.5  # type: ignore[misc]

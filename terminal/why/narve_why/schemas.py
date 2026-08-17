"""Contract 1 (CONTRACTS.md v1.0.0) request dataclasses and validation.

Hand-rolled validation so every failure carries the exact field path, e.g.
``model_outputs[2].p: must be within [0, 1] (got 1.4)``. Julian debugs the
prediction side off these strings — the format is part of the product.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "ContractError",
    "ExplainRequest",
    "MarketSnapshot",
    "ModelOutput",
    "parse_request",
]


class ContractError(ValueError):
    """Contract violation. Message: ``{field_path}: {reason} (got {value!r})``."""

    field_path: str

    def __init__(self, field_path: str, reason: str, value: object) -> None:
        self.field_path = field_path
        super().__init__(f"{field_path}: {reason} (got {value!r})")


@dataclass(frozen=True)
class ModelOutput:
    model_id: str
    source_id: str
    p: float
    weight: float
    inputs_ref: tuple[str, ...]


@dataclass(frozen=True)
class MarketSnapshot:
    venue: str
    yes_price: float
    liquidity: float | None
    captured_at: str


@dataclass(frozen=True)
class ExplainRequest:
    question_id: str
    question_text: str
    probability: float
    as_of: str
    model_outputs: tuple[ModelOutput, ...]
    market_snapshots: tuple[MarketSnapshot, ...]


def _require(raw: dict[str, Any], key: str, path: str) -> Any:
    if key not in raw:
        raise ContractError(path, "missing required field", None)
    return raw[key]


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(path, "must be a string", value)
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(path, "must be a number", value)
    return float(value)


def _unit_interval(value: Any, path: str) -> float:
    number = _number(value, path)
    if not 0.0 <= number <= 1.0:
        raise ContractError(path, "must be within [0, 1]", value)
    return number


def _timestamp(value: Any, path: str) -> str:
    reason = "must be an ISO-8601 UTC timestamp"
    if not isinstance(value, str):
        raise ContractError(path, reason, value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ContractError(path, reason, value) from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ContractError(path, reason, value)
    return value


def _parse_model_output(entry: Any, path: str) -> ModelOutput:
    if not isinstance(entry, dict):
        raise ContractError(path, "must be an object", entry)
    model_id = _string(_require(entry, "model_id", f"{path}.model_id"), f"{path}.model_id")
    source_id = _string(_require(entry, "source_id", f"{path}.source_id"), f"{path}.source_id")
    p = _unit_interval(_require(entry, "p", f"{path}.p"), f"{path}.p")
    weight_raw = _require(entry, "weight", f"{path}.weight")
    weight = _number(weight_raw, f"{path}.weight")
    if weight < 0:
        raise ContractError(f"{path}.weight", "must be >= 0", weight_raw)
    refs_raw = _require(entry, "inputs_ref", f"{path}.inputs_ref")
    if not isinstance(refs_raw, list):
        raise ContractError(f"{path}.inputs_ref", "must be an array", refs_raw)
    if not refs_raw:
        raise ContractError(f"{path}.inputs_ref", "must be non-empty", refs_raw)
    inputs_ref = tuple(
        _string(ref, f"{path}.inputs_ref[{index}]") for index, ref in enumerate(refs_raw)
    )
    return ModelOutput(
        model_id=model_id, source_id=source_id, p=p, weight=weight, inputs_ref=inputs_ref
    )


def _parse_snapshot(entry: Any, path: str) -> MarketSnapshot:
    if not isinstance(entry, dict):
        raise ContractError(path, "must be an object", entry)
    venue = _string(_require(entry, "venue", f"{path}.venue"), f"{path}.venue")
    yes_price = _unit_interval(_require(entry, "yes_price", f"{path}.yes_price"), f"{path}.yes_price")
    liquidity_raw = entry.get("liquidity")
    liquidity = None if liquidity_raw is None else _number(liquidity_raw, f"{path}.liquidity")
    captured_at = _timestamp(
        _require(entry, "captured_at", f"{path}.captured_at"), f"{path}.captured_at"
    )
    return MarketSnapshot(
        venue=venue, yes_price=yes_price, liquidity=liquidity, captured_at=captured_at
    )


def parse_request(raw: dict[str, Any]) -> ExplainRequest:
    """Validate a raw ExplainRequest payload. Unknown fields are ignored.

    Raises ContractError with the exact offending field path on any violation.
    """
    if not isinstance(raw, dict):
        raise ContractError("$", "must be an object", raw)
    question_id = _string(_require(raw, "question_id", "question_id"), "question_id")
    question_text = _string(_require(raw, "question_text", "question_text"), "question_text")
    probability = _unit_interval(_require(raw, "probability", "probability"), "probability")
    as_of = _timestamp(_require(raw, "as_of", "as_of"), "as_of")
    outputs_raw = _require(raw, "model_outputs", "model_outputs")
    if not isinstance(outputs_raw, list):
        raise ContractError("model_outputs", "must be an array", outputs_raw)
    if not outputs_raw:
        raise ContractError("model_outputs", "must be non-empty", outputs_raw)
    model_outputs = tuple(
        _parse_model_output(entry, f"model_outputs[{index}]")
        for index, entry in enumerate(outputs_raw)
    )
    snapshots_raw = raw.get("market_snapshots", [])
    if not isinstance(snapshots_raw, list):
        raise ContractError("market_snapshots", "must be an array", snapshots_raw)
    market_snapshots = tuple(
        _parse_snapshot(entry, f"market_snapshots[{index}]")
        for index, entry in enumerate(snapshots_raw)
    )
    return ExplainRequest(
        question_id=question_id,
        question_text=question_text,
        probability=probability,
        as_of=as_of,
        model_outputs=model_outputs,
        market_snapshots=market_snapshots,
    )

"""Golden-dataset loader for the walk-forward backtest harness.

Pure JSON + validation. **No app imports** — this module is safe to import from
tests, scripts, and the replay harness without dragging in the FastAPI app, the
DB, or any config. The only contract it depends on is documented in
``gateway/data/backtest/SCHEMA.md``.

Public API
----------
    load_dataset(path) -> list[dict]
        Load a golden-dataset JSON file, validate every record against the
        schema and the no-lookahead invariants, and return a list of market
        dicts. Raises ``DatasetError`` with a clear, located message on any
        malformed record.

The dataset file may be either a top-level JSON array of market records, or an
object with a ``"markets"`` key. Both yield the same ``list[dict]`` return.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Union


__all__ = ["load_dataset", "DatasetError", "parse_iso_date"]


class DatasetError(ValueError):
    """Raised when a golden-dataset file is missing, unparseable, or violates
    the schema / no-lookahead invariants documented in SCHEMA.md."""


# ── date parsing ─────────────────────────────────────────────────────────────


def parse_iso_date(value: Any) -> date:
    """Parse an ISO date or datetime string into a ``date``.

    Accepts ``"2024-11-06"`` and full ISO 8601 datetimes like
    ``"2024-11-06T12:30:00"`` / ``"2024-11-06T12:30:00Z"``. Raises ``ValueError``
    on anything else (caller wraps this into a located ``DatasetError``).
    """
    if not isinstance(value, str):
        raise ValueError(f"expected an ISO date string, got {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError("empty date string")
    # Plain date fast-path.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    # Full datetime (tolerate a trailing Z for UTC).
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    return datetime.fromisoformat(iso).date()


# ── field helpers ────────────────────────────────────────────────────────────


def _require(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise DatasetError(f"{where}: missing required field '{key}'")
    return obj[key]


def _as_float(value: Any, key: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetError(f"{where}: '{key}' must be a number, got {value!r}")
    return float(value)


def _as_str(value: Any, key: str, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DatasetError(f"{where}: '{key}' must be a string, got {value!r}")
    if not allow_empty and not value.strip():
        raise DatasetError(f"{where}: '{key}' must be a non-empty string")
    return value


def _as_date(value: Any, key: str, where: str) -> date:
    try:
        return parse_iso_date(value)
    except ValueError as exc:
        raise DatasetError(f"{where}: '{key}' is not a parseable ISO date ({value!r}): {exc}")


def _in_unit_interval(value: float, key: str, where: str) -> float:
    if not (0.0 <= value <= 1.0):
        raise DatasetError(f"{where}: '{key}' must be in [0, 1], got {value}")
    return value


# ── record validation ────────────────────────────────────────────────────────


def _validate_price_point(point: Any, resolved_at: date, where: str) -> None:
    if not isinstance(point, dict):
        raise DatasetError(f"{where}: each price_timeline item must be an object, got {point!r}")
    d = _as_date(_require(point, "date", where), "date", where)
    yp = _in_unit_interval(_as_float(_require(point, "yes_price", where), "yes_price", where), "yes_price", where)
    point["yes_price"] = yp  # normalize to float
    if d > resolved_at:
        raise DatasetError(
            f"{where}: price_timeline date {d.isoformat()} is after resolved_at "
            f"{resolved_at.isoformat()} (lookahead — prices must not be post-resolution)"
        )


def _validate_forecast(fc: Any, resolved_at: date, where: str) -> None:
    if not isinstance(fc, dict):
        raise DatasetError(f"{where}: each forecast must be an object, got {fc!r}")
    _as_str(_require(fc, "source_handle", where), "source_handle", where)
    pp = _in_unit_interval(
        _as_float(_require(fc, "predicted_probability", where), "predicted_probability", where),
        "predicted_probability",
        where,
    )
    fc["predicted_probability"] = pp  # normalize to float
    made_at = _as_date(_require(fc, "made_at", where), "made_at", where)
    # url is required by the schema but may be an empty string (audit trail optional).
    _as_str(_require(fc, "url", where), "url", where, allow_empty=True)
    if made_at >= resolved_at:
        raise DatasetError(
            f"{where}: forecast made_at {made_at.isoformat()} is not strictly before "
            f"resolved_at {resolved_at.isoformat()} (LOOKAHEAD — the bug that invalidates "
            f"backtests; forecasts must predate resolution)"
        )


def _validate_market(market: Any, index: int, seen_ids: set[str]) -> dict:
    where0 = f"market[{index}]"
    if not isinstance(market, dict):
        raise DatasetError(f"{where0}: each market record must be an object, got {market!r}")

    market_id = _as_str(_require(market, "market_id", where0), "market_id", where0)
    where = f"market[{index}] ({market_id})"

    if market_id in seen_ids:
        raise DatasetError(f"{where}: duplicate market_id '{market_id}' (must be unique across the dataset)")
    seen_ids.add(market_id)

    _as_str(_require(market, "question", where), "question", where)

    outcome = _require(market, "resolved_outcome", where)
    if isinstance(outcome, bool) or outcome not in (0, 1):
        raise DatasetError(f"{where}: 'resolved_outcome' must be exactly 0 or 1, got {outcome!r}")

    resolved_at = _as_date(_require(market, "resolved_at", where), "resolved_at", where)

    timeline = _require(market, "price_timeline", where)
    if not isinstance(timeline, list) or not timeline:
        raise DatasetError(f"{where}: 'price_timeline' must be a non-empty list")
    for i, point in enumerate(timeline):
        _validate_price_point(point, resolved_at, f"{where}.price_timeline[{i}]")

    forecasts = _require(market, "forecasts", where)
    if not isinstance(forecasts, list) or not forecasts:
        raise DatasetError(f"{where}: 'forecasts' must be a non-empty list")
    for i, fc in enumerate(forecasts):
        _validate_forecast(fc, resolved_at, f"{where}.forecasts[{i}]")

    return market


# ── public entrypoint ────────────────────────────────────────────────────────


def load_dataset(path: Union[str, Path]) -> list[dict]:
    """Load and validate a golden-dataset JSON file.

    Args:
        path: path to a dataset file. The file is either a JSON array of market
            records, or an object with a ``"markets"`` key. See SCHEMA.md.

    Returns:
        A list of validated market dicts (numeric fields normalized to float).

    Raises:
        DatasetError: file missing, not valid JSON, wrong top-level shape, or any
            record violating the schema / no-lookahead invariants. The message
            names the offending record and field.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DatasetError(f"dataset file not found: {p}")
    except OSError as exc:
        raise DatasetError(f"could not read dataset file {p}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset file {p} is not valid JSON: {exc}")

    if isinstance(data, dict):
        if "markets" not in data:
            raise DatasetError(
                f"dataset file {p}: object form must have a 'markets' key holding the list of records"
            )
        markets = data["markets"]
    elif isinstance(data, list):
        markets = data
    else:
        raise DatasetError(
            f"dataset file {p}: top level must be a JSON array of markets or an object with a 'markets' key, "
            f"got {type(data).__name__}"
        )

    if not isinstance(markets, list):
        raise DatasetError(f"dataset file {p}: 'markets' must be a list, got {type(markets).__name__}")
    if not markets:
        raise DatasetError(f"dataset file {p}: dataset is empty (no markets)")

    seen_ids: set[str] = set()
    return [_validate_market(m, i, seen_ids) for i, m in enumerate(markets)]


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).parent / "data" / "backtest" / "synthetic_fixture.json"
    )
    rows = load_dataset(target)
    print(f"loaded {len(rows)} markets from {target}")
    for m in rows:
        print(f"  - {m['market_id']}: {len(m['forecasts'])} forecasts, "
              f"{len(m['price_timeline'])} price points, outcome={m['resolved_outcome']}")

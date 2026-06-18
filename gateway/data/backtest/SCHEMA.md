# Golden-dataset record schema — narve walk-forward backtest

**Status:** contract (the user fills this with real data)
**Consumed by:** `gateway/backtest_dataset.py` → `load_dataset(path)` → replay harness
**Spec:** `docs/superpowers/specs/2026-06-18-walk-forward-backtest-demo-design.md` §"1. Golden dataset"

This file is the **single source of truth** for the JSON record shape. The replay
harness (`gateway/backtest_replay.py`) and the loader build to exactly this. Do not
add or rename fields without updating this file and the loader's validation together.

---

## Top-level shape

A dataset file is **either**:

- a JSON array of market records: `[ {market}, {market}, ... ]`, **or**
- a JSON object with a `"markets"` key: `{ "markets": [ {market}, ... ] }`
  (optional sibling metadata keys like `"description"` are allowed and ignored
  by the loader).

The loader accepts both forms and always returns a **`list` of market dicts**.

---

## Market record

Each market record is a JSON object with these fields. **All fields are required**
unless marked optional.

| field              | type             | notes                                                           |
| ------------------ | ---------------- | --------------------------------------------------------------- |
| `market_id`        | string           | stable unique id / slug (e.g. Polymarket/Kalshi slug). Unique across the dataset. |
| `question`         | string           | the market question, human-readable.                            |
| `resolved_outcome` | integer `1`/`0`  | `1` = YES resolved true, `0` = NO resolved true.                |
| `resolved_at`      | string (ISO date)| date the outcome was known, e.g. `"2024-11-06"` or full ISO 8601 datetime. |
| `price_timeline`   | array of objects | the market's YES-price history. See **Price point** below. Non-empty. |
| `forecasts`        | array of objects | dated forecaster predictions made **before** `resolved_at`. See **Forecast** below. Non-empty. |

### Price point (item of `price_timeline`)

| field       | type              | notes                                              |
| ----------- | ----------------- | -------------------------------------------------- |
| `date`      | string (ISO date) | date of this price observation.                    |
| `yes_price` | float in `[0, 1]` | market-implied probability of YES on that date.    |

`price_timeline` must be **chronologically sortable** and every `date` must be
**on or before** `resolved_at` (no post-resolution prices).

### Forecast (item of `forecasts`)

| field                   | type              | notes                                                          |
| ----------------------- | ----------------- | -------------------------------------------------------------- |
| `source_handle`         | string            | the forecaster's handle (e.g. `@nate_silver`).                 |
| `predicted_probability` | float in `[0, 1]` | the forecaster's stated probability of YES.                    |
| `made_at`               | string (ISO date) | when the forecast was made. **Must be strictly before** `resolved_at` (no-lookahead guard). |
| `url`                   | string            | link to the source post/article (audit trail). May be empty string if unavailable. |

---

## No-lookahead invariants (enforced by the loader)

These are the integrity guards from the spec. The loader **hard-fails** with a
clear error if any are violated:

1. Every `forecasts[].made_at` is **strictly before** the market's `resolved_at`.
   (A forecast made at/after resolution is lookahead — the bug that invalidates
   backtests.)
2. Every `price_timeline[].date` is **on or before** `resolved_at`.
3. All dates (`resolved_at`, `made_at`, `date`) are parseable ISO dates.
4. `resolved_outcome` is exactly `0` or `1`.
5. `predicted_probability` and `yes_price` are within `[0, 1]`.
6. `market_id` is unique across the dataset.

The harness adds a second, **per-decision** lookahead assertion at replay time
(no input timestamp ≥ the decision date); the loader guarantees only the
dataset-level invariants above.

---

## Example record

```json
{
  "market_id": "presidential-election-winner-2024",
  "question": "Will the Democratic candidate win the 2024 US presidential election?",
  "resolved_outcome": 0,
  "resolved_at": "2024-11-06",
  "price_timeline": [
    { "date": "2024-09-01", "yes_price": 0.52 },
    { "date": "2024-10-01", "yes_price": 0.55 },
    { "date": "2024-11-04", "yes_price": 0.47 }
  ],
  "forecasts": [
    {
      "source_handle": "@forecaster_a",
      "predicted_probability": 0.40,
      "made_at": "2024-09-15",
      "url": "https://example.com/post/123"
    },
    {
      "source_handle": "@forecaster_b",
      "predicted_probability": 0.60,
      "made_at": "2024-10-20",
      "url": "https://example.com/post/456"
    }
  ]
}
```

---

## Mapping to the existing `simulate()` engine (`gateway/backtest.py`)

The golden dataset is **ISO-dated and human-curated**. The replay harness
translates it into the per-prediction dicts `simulate()` expects (unix-seconds
timestamps, `market_price_at_prediction`, `resolved_correct`, etc.). This file
intentionally does **not** mirror the engine's internal field names — it is the
clean human contract; the harness owns the translation.

Engine-side correspondence (for the harness author):

| dataset field                       | engine field (per-prediction dict)        |
| ----------------------------------- | ----------------------------------------- |
| `forecasts[].predicted_probability` | `predicted_probability`                   |
| `forecasts[].source_handle`         | `source_handle`                           |
| `forecasts[].made_at` (→ unix)      | `extracted_at`                            |
| price at decision date              | `market_price_at_prediction` / `yes_price`|
| `resolved_outcome` vs. bet side     | `resolved_correct` (bool)                 |
| `question`                          | `content`                                 |
| `resolved_at` (→ unix)              | used for credibility time-decay + scoring |

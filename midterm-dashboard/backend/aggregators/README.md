# midterm-dashboard/backend/aggregators/ — Source connectors

One module per data source. Each aggregator class fetches the latest market /
poll data and normalizes it into the unified schema the rest of the backend
expects, so `main.py` and `database.py` don't care which source a row came
from.

Aggregators are imported via the package `__init__.py` and instantiated
inside the background refresh loop in `main.py`.

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `PolymarketAggregator`, `KalshiAggregator`, `PredictItAggregator`, `PollingAggregator` so `from aggregators import *` Just Works. |
| `polymarket.py` | `PolymarketAggregator` — pulls 2026 midterm markets from `gamma-api.polymarket.com`, maps title patterns to `(race_type, state)` keys, returns normalized rows. |
| `kalshi.py` | `KalshiAggregator` — pulls election event markets from `api.elections.kalshi.com/trade-api/v2`. Same output shape as the Polymarket one. |
| `predictit.py` | `PredictItAggregator` — wraps `predictit.org/api/marketdata/all/`. Maintained as backup data; PredictIt has been winding down US election markets. |
| `polling.py` | `PollingAggregator` — pulls polling averages from public sources (538, RealClearPolitics-style aggregations). Used as a sanity-check baseline against the prediction-market prices. |

Adding a new source: subclass the (informal) aggregator interface — `async
def fetch_all() -> list[dict]` returning normalized rows — and wire it into
`__init__.py` and the refresh loop in `main.py`.

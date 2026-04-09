# app/markets/ — Prediction market clients

Async HTTP clients for the two prediction markets the dashboard cross-references
extracted predictions against. Both clients are read-only — the dashboard never
places trades.

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `polymarket.py` | `PolymarketClient` — wraps `https://gamma-api.polymarket.com`. Fetches active markets, filters by `category_keywords` from `config.yaml`, returns normalized market data. Used by `processing/resolver.py` to settle predictions. |
| `kalshi.py` | `KalshiClient` — wraps `https://api.elections.kalshi.com/trade-api/v2`. Same interface as the Polymarket client, returns Kalshi-format markets normalized into the same shape so the rest of the pipeline doesn't care which exchange a market came from. |

Both clients pull `category_keywords` and HTTP timeouts from `app/config.yaml`.

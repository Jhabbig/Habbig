# Crypto Trackers Dashboard

Best-in-class crypto data infrastructure. Every coin, every tracker, with
verifiable timestamps and per-source latency. **Data fidelity first** — no
neural-net predictions, no fortune-telling; every datapoint is mirrored from
canonical exchange / on-chain source-of-truth.

Pairs with `crypto-dashboard` (ML signals + Polymarket divergence) — this
dashboard is the **trackers** product, the long-tail data layer that
matters most to traders in the long run.

Port: **7054**. Lives behind the gateway at `trackers.narve.ai` in production.

## What's built (v0)

| Panel | Source | Refresh |
|---|---|---|
| **Critical strip** (11 tiles) | Aggregated from CoinGecko + DefiLlama + F&G | 60 s |
| **Fear & Greed gauge** with 7-day strip | Alternative.me | 1 h |
| **Universe screener** with filters + sort | CoinGecko top 500 by market cap | 60 s |
| **Top gainers/losers (24h)** | CoinGecko universe slice | 60 s |
| **Cross-exchange arbitrage** Binance/Coinbase/Kraken/Bybit/OKX | Per-exchange tickers joined on normalised base symbol; net of round-trip taker fees | 60 s |
| **Funding rates** (USDT perps, Binance+Bybit) | Binance premiumIndex + Bybit V5 tickers | 30 s |
| **DeFi TVL** chains + stablecoins | DefiLlama `/v2/chains`, `/protocols`, `/stablecoins`, `/overview/dexs` | 15 min |
| **Trending coins** | CoinGecko `/search/trending` | 5 min |
| **Per-source health** | In-process latency EMA + success rate | 30 s |
| **Disk-persisted cache** | YTD-grade survival across process restarts | n/a |

## Why this dashboard (the strategy)

The product thesis: **in the long run, the most valuable features aren't
the ML predictions, they're the data trackers themselves** — speed,
breadth, fidelity. Every competing product (CoinMarketCap, CoinGecko,
TradingView, DexScreener, DefiLlama, Whale Alert) has built its moat on
data infrastructure, not on prediction accuracy. This dashboard ships
those trackers, for every coin, in one place, with verifiable timestamps.

**100% data fidelity is achievable** (mirror source-of-truth feeds, stamp
every datapoint with source + latency). **100% prediction accuracy is
not** (stochastic markets) — so we don't pretend to offer it. The
existing `crypto-dashboard` continues to ship the ML signal product;
this dashboard ships the data product.

## Run locally

```bash
cd crypto-trackers-dashboard
cp .env.example .env       # DEV_MODE=1 lets you skip gateway auth
pip install -r requirements.txt
python3 server.py
# → http://localhost:7054
```

Or via Docker from the repo root:

```bash
docker compose up --build crypto-trackers
```

Smoke-test individual modules:

```bash
python3 -m ingestion.coingecko
python3 -m ingestion.binance
python3 -m ingestion.coinbase
python3 -m ingestion.kraken
python3 -m ingestion.bybit
python3 -m ingestion.okx
python3 -m ingestion.defillama
python3 -m ingestion.fear_greed
```

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /healthz` | — | Liveness probe (bypasses SSO) |
| `GET /api/health` | — | Same as `/healthz` but goes through SSO |
| `GET /api/summary` | per-feed | Single payload for the front page |
| `GET /api/universe?top_n=500` | 60 s | Top-N coins by market cap |
| `GET /api/screener?...` | 60 s | Filtered + sorted universe; query params: `min_market_cap`, `min_volume`, `min_change_24h`, `max_change_24h`, `search`, `sort`, `order`, `limit` |
| `GET /api/coin/{id}` | 2 min | Full per-coin detail (description, links, market data, sparkline) |
| `GET /api/global` | 2 min | Total market cap + BTC/ETH dominance + active coins |
| `GET /api/trending` | 5 min | CoinGecko 24h trending (by search volume) |
| `GET /api/binance/spot` | 30 s | Every Binance spot 24h ticker |
| `GET /api/binance/futures` | 30 s | Every Binance futures 24h ticker |
| `GET /api/binance/depth?symbol=&limit=` | 5 s | L2 orderbook snapshot |
| `GET /api/binance/klines?symbol=&interval=&limit=` | 60 s | OHLCV klines |
| `GET /api/coinbase/{product_id}/ticker` | 10 s | Coinbase price + bid/ask |
| `GET /api/coinbase/{product_id}/stats` | 60 s | 24h + 30d Coinbase stats |
| `GET /api/kraken/ticker?pair=` | 10 s | Kraken price + bid/ask + 24h o/h/l/v |
| `GET /api/bybit/tickers?category=` | 30 s | Bybit V5 spot or linear tickers |
| `GET /api/okx/tickers?inst_type=` | 30 s | OKX V5 SPOT/SWAP tickers |
| `GET /api/defi/chains` | 15 min | DefiLlama per-chain TVL |
| `GET /api/defi/protocols?limit=` | 15 min | DefiLlama top protocols |
| `GET /api/defi/stablecoins` | 30 min | DefiLlama stablecoin supplies |
| `GET /api/defi/dexs` | 15 min | DefiLlama DEX volume overview |
| `GET /api/sentiment/fear_greed?days=` | 1 h | Alternative.me Fear & Greed |
| `GET /api/cross_exchange/spreads?min_volume_usd=&top_n=` | 60 s | Cross-exchange arbitrage scanner |
| `GET /api/funding/rates` | 30 s | Funding-rate aggregator (Binance + Bybit) |
| `GET /api/sources` | live | Per-upstream health + persisted-cache view |

## Files

```
crypto-trackers-dashboard/
├── server.py                     FastAPI app + SSO middleware + 25+ routes + asyncio.gather
├── ingestion/
│   ├── _cache.py                 In-process TTL + disk-persistence fallback
│   ├── _persistence.py           Atomic-rename disk cache
│   ├── _http.py                  Polite UA + per-source health recording
│   ├── _health.py                Per-source GREEN/YELLOW/RED + latency EMA
│   ├── _background.py            Daemon-thread pre-fetch loop (opt-in)
│   ├── coingecko.py              Universe + per-coin + global + trending
│   ├── binance.py                Spot + futures + depth + klines + funding
│   ├── coinbase.py               Coinbase Exchange (ticker + stats)
│   ├── kraken.py                 Kraken (ticker)
│   ├── bybit.py                  Bybit V5 (spot + linear perps + funding)
│   ├── okx.py                    OKX V5 (spot + swap + funding)
│   ├── defillama.py              TVL chains + protocols + stablecoins + DEXs
│   └── fear_greed.py             Alternative.me Crypto F&G Index
├── analysis/
│   ├── arbitrage.py              Cross-exchange spread scanner (net of fees)
│   ├── funding.py                Funding-rate aggregator across venues
│   └── screener.py               Universe filter / sort helper
├── index.html                    Single-file UI (no build step, no JS deps):
│                                 crit strip + F&G gauge + screener + movers +
│                                 cross-exchange + funding + DeFi + trending +
│                                 source health
├── Dockerfile                    Python 3.12-slim, non-root, port 7054
├── requirements.txt              fastapi, uvicorn, requests
├── .env.example
├── .dockerignore
└── README.md                     (this file)
```

## Roadmap

| Step | Status | Adds |
|---|---|---|
| v0   | ✓ done | Universe + multi-exchange + cross-arb + funding + DeFi + F&G + source health |
| v0.1 | open | Liquidation heatmap (Coinglass-style aggregation across venues) |
| v0.2 | open | Whale-transaction tracker (Whale Alert mirror + exchange in/outflows) |
| v0.3 | open | On-chain context (Etherscan / Solscan / Basescan basic stats per coin) |
| v0.4 | open | News aggregator (CoinDesk + Decrypt + The Block + crypto.news RSS) |
| v0.5 | open | Network metrics (hash rate / gas / mempool depth / miner flows) |
| v0.6 | open | Per-coin detail page with TradingView-style candlestick chart |
| v0.7 | open | Token unlocks calendar + IDO / new listings calendar |
| v0.8 | open | Smart-money wallet tracker (top wallet PnL leaderboard) |
| v0.9 | open | MEV scanner (sandwich + frontrun detection) |
| v1.0 | open | WebSocket push for sub-second price updates |
| v1.1 | open | Custom alerts (price / volume / funding-rate / spread thresholds) |
| v1.2 | open | Portfolio tracking + simulated DCA / pair-trade strategies |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7054` | Override listen port. |
| `BIND_HOST` | `0.0.0.0` | Override bind host. |
| `CT_PREFETCH` | unset | Set `1` to enable the background pre-fetch loop. |
| `CT_CACHE_DIR` | `./cache/` | Override the disk-cache directory. |

## Caveats / known limits

- **Cross-exchange "arb" is not financial advice.** Reported spreads are
  raw mid-vs-mid. Real-world arb after withdrawal fees + on-chain latency
  + venue-specific fee tiers is usually negative on majors. The dashboard
  flags spreads net of round-trip taker fees only; the user is on their
  own for withdrawal/slippage.
- **Kraken pair coverage is partial** — we only query a curated set of
  USD pairs (BTC/ETH/SOL/DOGE/XRP/ADA/AVAX/DOT/LINK). A v0.x can pull
  Kraken's full `/0/public/AssetPairs` and screen for the top-volume
  pairs.
- **No DEX prices yet.** v0.x adds Uniswap V3 / Curve / Aerodrome pool
  prices via DefiLlama's `/coins/prices/current` endpoint or direct
  on-chain reads. Critical for stablecoin de-peg signals.
- **Funding-rate aggregator** only covers Binance + Bybit USDT perps.
  Adding OKX funding requires per-instrument calls (the bulk
  `/api/v5/market/tickers` doesn't include funding) — v0.x.
- **No alerts yet.** Custom price/spread/funding thresholds with email
  or webhook delivery is v1.1.
- **AirNow-style key-gated endpoints aren't used here** — every source
  in this dashboard is fully public, no key required. That's deliberate;
  keys are a deployment-tax issue and the public endpoints cover the
  trackers we ship.

# Crypto Trackers Dashboard

Best-in-class crypto data infrastructure. Every coin, every tracker, with
verifiable timestamps and per-source latency. **Data fidelity first** — no
neural-net predictions, no fortune-telling; every datapoint is mirrored from
canonical exchange / on-chain source-of-truth.

Pairs with `crypto-dashboard` (ML signals + Polymarket divergence) — this
dashboard is the **trackers** product, the long-tail data layer that
matters most to traders in the long run.

Port: **7054**. Lives behind the gateway at `trackers.narve.ai` in production.

## What's built (v0.2)

**17 upstream feeds**, all free / no API key required (Etherscan optional).

| Panel | Source | Refresh |
|---|---|---|
| **Critical strip** (11 tiles) | Aggregated from CoinGecko + DefiLlama + F&G | 60 s |
| **Fear & Greed gauge** with 7-day strip | Alternative.me | 1 h |
| **Universe screener** (clickable rows → per-coin page) with filters + sort | CoinGecko top 500 | 60 s |
| **Top gainers/losers (24h)** (clickable → per-coin page) | CoinGecko universe slice | 60 s |
| **Cross-exchange arbitrage** 5-venue | Per-exchange tickers, normalised, net of round-trip fees | 60 s |
| **Funding rates** (USDT perps) | Binance premiumIndex + Bybit V5 tickers | 30 s |
| **DeFi TVL** chains + stablecoins + DEXs | DefiLlama suite | 15 min |
| **Trending coins** | CoinGecko `/search/trending` | 5 min |
| **News aggregator** (7 outlets, deduped) | RSS multi-feed | 10 min |
| **BTC network metrics** (fees, mempool, tip, difficulty adj.) | mempool.space | 60 s |
| **BTC hashrate** (3-day series) | mempool.space mining endpoint | 1 h |
| **ETH gas oracle** | Etherscan | 60 s |
| **SOL network** (TPS avg/peak, epoch progress, slot) | Solana mainnet-beta RPC | 30 s |
| **SOL priority-fee market** (median / p90 / p99 / max lamports) | `getRecentPrioritizationFees` | 30 s |
| **Multi-venue liquidations** | Binance `allForceOrders` + OKX `/public/liquidation-orders`, joined on normalised base | 60 s |
| **Cross-DEX spot prices** (17 tokens, 6 chains) | DefiLlama `/coins/prices/current` | 60 s |
| **BTC treasuries** (ETFs + public co's + govts) | Curated 2025-Q3 snapshot | n/a |
| **Per-source health** | In-process latency EMA + success rate | 30 s |
| **Disk-persisted cache** | Survives restarts | n/a |

## Per-coin detail page (`/coin?id=`)

Click any row in the universe (or movers) tables to drill into a full
per-coin page with **everything a serious trader needs in one view**:

  - **Header stats**: price + 24h/7d/30d/1y change + market cap + ATH/ATL +
    circulating supply, formatted CMC/CoinGecko-style.
  - **Inline-SVG candlestick chart**: OHLCV candles with 20-period EMA
    overlay, volume bars beneath, live price marker on the right axis,
    selectable interval (15m / 1h / 4h / 1d / 1w). No external charting
    library; pure SVG renders in milliseconds.
  - **L2 order book**: Binance spot bids + asks with cumulative size and
    side-shaded background (green for bids, red for asks).
  - **Cross-venue spot prices**: this coin's price on Binance + Coinbase +
    Kraken + Bybit + OKX with CHEAP / RICH flags + spread %.
  - **Funding rate card**: median 8h rate + annualised + venue spread for
    this coin's perp.
  - **News mentioning this coin**: filtered from the 7-source RSS feed by
    symbol + name substring match.
  - **About card**: CoinGecko categories + description + homepage + Twitter.

Every section auto-refreshes (chart every 60s, depth every 30s).

## What's new in v0.2

- **Per-coin detail page** with candlestick chart, depth ladder, cross-venue
  prices, funding, news, about. Universe and movers tables now click
  through to it.
- **Multi-venue liquidations** — added OKX `/public/liquidation-orders`
  and aggregated with Binance into a single coin-keyed table (long-liq /
  short-liq / total / count, sorted by total notional).
- **Solana network metrics** — TPS recent average + peak, epoch progress,
  slot height, slots-remaining-in-epoch. Plus `getRecentPrioritizationFees`
  with median / p90 / p99 / max lamports.
- **Network grid** in the home page now shows BTC + ETH + SOL side by
  side (was BTC + ETH only).

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
| `GET /api/news?limit=` | 10 min | Aggregated headlines (7 RSS sources, deduped) |
| `GET /api/network/btc` | 60 s | BTC fees + mempool + tip + difficulty adjustment |
| `GET /api/network/btc/hashrate` | 1 h | 3-day BTC hashrate series |
| `GET /api/network/eth/gas` | 60 s | ETH gas oracle (gwei: safe / propose / fast / base) |
| `GET /coin?id=` | — | Per-coin detail page UI (HTML) |
| `GET /api/liquidations/binance` | 60 s | Binance USDT-perp liquidations |
| `GET /api/liquidations/okx` | 60 s | OKX swap liquidations |
| `GET /api/liquidations/aggregate` | 60 s | Joined Binance + OKX per-coin liquidations |
| `GET /api/network/btc` | 60 s | BTC fees + mempool + tip + difficulty adjustment |
| `GET /api/network/btc/hashrate` | 1 h | 3-day BTC hashrate series |
| `GET /api/network/eth/gas` | 60 s | ETH gas oracle (gwei: safe / propose / fast / base) |
| `GET /api/network/sol` | 30 s | Solana slot + epoch + TPS recent avg/peak |
| `GET /api/network/sol/fees` | 30 s | Solana priority-fee market (median/p90/p99/max lamports) |

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
| v0.1 | ✓ done | News aggregator (7 RSS sources) + BTC/ETH network metrics + Binance liquidations + cross-DEX prices + BTC treasuries |
| v0.2 | ✓ done | Per-coin detail page (candlestick + depth + cross-venue + news + funding); multi-venue liquidations (+ OKX); Solana RPC metrics + priority fees |
| v0.3 | ✓ done | Whale tracker: Etherscan exchange-wallet ETH balances + delta detection across 14 hot/cold wallets; mempool.space 100 BTC+ unconfirmed-tx feed |
| v0.4 | open | On-chain context per coin (Etherscan / Solscan / Basescan tx + holder counts) |
| v0.5 | open | Add Hyperliquid + Bybit to liquidation aggregator (Bybit needs websocket) |
| v0.6 | open | Solana validator stake distribution + Jito tip stream |
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

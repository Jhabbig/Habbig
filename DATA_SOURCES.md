# Data sources — the complete map

Every feed the fleet consumes, per dashboard: where the data comes from,
whether it needs a credential, and how it refreshes. Run
`python3 bootstrap_data.py` on the production box to probe every upstream,
audit the keys, and get a per-dashboard data-readiness report.

**The big picture:** almost everything is a free, keyless public API fetched
at runtime by each dashboard's own poller/ingest workers. Only four
credentials gate real functionality — `ODDS_API_KEY` (sports signals),
`ANTHROPIC_API_KEY` (the LLM extraction stage), X/TruthSocial credentials
(truth scraping), and Alpaca keys (stock desk live quotes). `SEC_USER_AGENT`
looks like a key but is just a contact string — set it and Whale Watch works.

## Per-dashboard feeds

| Dashboard | Feed | Host | Key? |
|---|---|---|---|
| **Market Edge — Crypto** (8000) | 5-min/1-sec klines | `api.binance.com`, `fapi.binance.com` | no |
| | Prediction markets | `gamma-api.polymarket.com`, `clob.polymarket.com` | no (key only to trade) |
| | Kalshi event markets | `api.elections.kalshi.com` | no |
| **Market Edge — Stocks** (8050) | Live quotes/bars | `data.alpaca.markets` | **ALPACA keys** |
| | Fallback quotes | yfinance (`query1.finance.yahoo.com`) | no |
| **Midterm Predictor** (8051) | Odds | Polymarket + Kalshi (above) | no |
| | PredictIt / polling | `projects.fivethirtyeight.com`, `www.realclearpolling.com`, `www.metaculus.com` | no |
| | Campaign finance | `api.open.fec.gov` | free demo key |
| **Weather, Climate & Disasters** (5050) | Weather markets | Polymarket + Kalshi | no |
| | Forecast models | `api.weather.gov` (NWS) | no |
| | Hazards | `earthquake.usgs.gov`, `www.spc.noaa.gov`, `eonet.gsfc.nasa.gov`, `www.gdacs.org`, `www.tsunami.gov`, `volcano.si.edu`, `www.fema.gov`, `api.reliefweb.int` | no |
| | Drought/climate | `usdmdataservices.unl.edu`, FRED CSV | no |
| **Sharpe Sports** (8888) | Bookmaker consensus | `api.the-odds-api.com` | **ODDS_API_KEY** |
| | Market prices | Polymarket + Kalshi | no |
| | Schedules | `site.api.espn.com` | no |
| **World State** (7050) | News | BBC/Reuters/NYT/NPR/Guardian RSS | no |
| | Satellites | `celestrak.org` | no |
| | X posts overlay | `api.x.com` | X_BEARER_TOKEN (optional) |
| | Infrastructure map | vendored in `infrastructure_data.py` | — |
| **Top Traders** (8052) | Whale wallets/trades | `data-api.polymarket.com`, `lb-api.polymarket.com` | no |
| | On-chain | `api.etherscan.io`, `api.polygonscan.com` | free keys (optional) |
| **Central Bank Tracker** (7060) | Rates/statements | `www.federalreserve.gov`, `www.ecb.europa.eu`, `www.bankofengland.co.uk`, FRED CSV | no |
| | FX | `api.frankfurter.dev` | no |
| | FOMC markets | Polymarket | no |
| **Truth Research** (18789) | Posts | X (`api.x.com`), TruthSocial, Reddit RSS, Substack RSS | **X/TruthSocial creds** (RSS keyless) |
| | LLM extraction | `api.anthropic.com` | **ANTHROPIC_API_KEY** (regex-only without) |
| | Market matching | Polymarket + Kalshi | no |
| **AI Race** (7070) | Benchmarks | `huggingface.co`, `datasets-server.huggingface.co`, `www.swebench.com`, lab blogs | no |
| | AI markets | Polymarket | no |
| **Crypto Trackers** (7054) | Spot + perps | `api.binance.com`, `fapi.binance.com` + other exchange public APIs | no |
| | DeFi TVL | `api.llama.fi`, `coins.llama.fi` | no |
| | Fear & Greed | `api.alternative.me` | no |
| **Religion & Cults** (7062) | Markets | Polymarket (religion tag) | no |
| | News | Vatican press/news RSS, `www.uscirf.gov`, Reddit | no |
| **Whale Watch** (8053) | 13F / Form 4 / 13D | `www.sec.gov` (EDGAR) | **SEC_USER_AGENT** (free string) |
| | Congress trades | house/senate-stock-watcher S3 buckets, `disclosures-clerk.house.gov` | no |
| | CFTC CoT | `publicreporting.cftc.gov` | no |
| | CUSIP→ticker | `api.openfigi.com` | free key (optional, faster) |
| **Voters** (7051) | Elections/context | curated `data/political_context.yaml` (committed) + `api.census.gov`, `api.worldbank.org`, news RSS | no |
| **financial-matrix-toolkit** | 15 US large-caps, ~6y daily | yfinance → committed CSV cache → seeded synthetic | no |

## Credentials that actually matter

| Env var | Gates | Where to get it | Cost |
|---|---|---|---|
| `ODDS_API_KEY` | Sports signal feed (core of Sharpe Sports) | the-odds-api.com | free tier (500 req/mo) |
| `ANTHROPIC_API_KEY` | LLM extraction stage of Truth Research | console.anthropic.com | usage-based |
| `TWITTER_BEARER_TOKEN` | X scraping (Truth Research, World State) | developer.x.com | free tier is thin |
| `TRUTHSOCIAL_USERNAME/PASSWORD` | TruthSocial scraping | your account | free |
| `ALPACA_API_KEY/SECRET` | Stock desk live data | alpaca.markets | free paper account |
| `SEC_USER_AGENT` | EDGAR ingest (Whale Watch) | **not a key** — any `"Name email@x"` string | free |
| `OPENFIGI_API_KEY` | 10× faster CUSIP resolution | openfigi.com/api | free |
| `POLYMARKET_API_KEY` / `KALSHI_*` | **trading only** — all read feeds are public | polymarket/kalshi accounts | free |

Set them once in `gateway/.env.production` — the systemd units, docker-compose
services, and `start_dashboards.sh` all load that file.

## How data refreshes

- **Runtime pollers**: every dashboard fetches its own feeds on boot and on a
  poll loop (30s–6h depending on feed). No manual step — start the service,
  data arrives.
- **Gateway poller + Redis**: the gateway also polls each dashboard's main
  API endpoints, caches responses, and pushes SSE `data_updated` events.
- **Manual refresh**: `financial-matrix-toolkit` is the exception —
  `python main.py --refresh` fetches real prices via yfinance and commits a
  CSV cache (the committed cache is synthetic until run on a machine with
  open egress).

## Why this file exists

The Claude Code sandbox that assembled the fleet has an egress policy that
denies every market-data host (proxy answers 403 to CONNECT), so data could
not be pulled from there. The production box has open egress: run
`python3 bootstrap_data.py` after deploy to verify every feed and key, then
start the services.

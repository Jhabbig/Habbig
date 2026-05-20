# Whale Tracker Dashboard — TradFi Insider, Activist, M&A, Fund & Congress Signals

Tracks public filings to surface what finance whales are doing in public
markets:

- **Insider transactions** — SEC Form 4 (officers, directors, 10% owners)
- **Activist stakes** — SEC SC 13D / 13G (>5% beneficial ownership)
- **M&A announcements** — SEC 8-K Items 1.01 / 2.01 with keyword scoring
- **Fund quarterly holdings** — SEC Form 13F-HR (institutional managers >$100M AUM)
- **Congressional trades** — House + Senate periodic transaction reports (STOCK Act)

Free public data only — paid options-flow and dark-pool feeds are out of scope.

Port: **8053**. Lives behind the gateway at `whales.narve.ai` in production.

## Run locally

```bash
cd whale-tracker-dashboard
cp .env.example .env       # set DEV_MODE=1 for standalone
pip install -r requirements.txt
python3 server.py
# http://localhost:8053
```

To trigger an ingest pass without waiting 5 minutes (only allowed in DEV_MODE):

```bash
curl -X POST http://localhost:8053/api/admin/ingest-now
```

Or via Docker from the repo root:

```bash
docker compose up --build whales
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `server.py` | FastAPI app — routes, gateway SSO middleware (with `/healthz` exempt), 30s response cache, startup ingest task, SSE stream endpoint. |
| `ingest.py` | Background loop polling EDGAR Atom feeds for Form 4, SC 13D, SC 13G, 8-K every `INGEST_INTERVAL_S` seconds. Refreshes the CIK→ticker map and broadcasts an `ingest` event when new filings land. |
| `edgar.py` | Polite EDGAR client: rate-limited (10 req/sec cap), Atom feed parsing, per-filing index.json + primary-doc URL helpers. |
| `cik_ticker.py` | CIK→ticker lookup, cached from `https://www.sec.gov/files/company_tickers.json`. Used to enrich 13D/G and 8-K rows where the filing index doesn't carry a ticker. |
| `events.py` | In-process pub/sub for the SSE live stream — bounded queues per subscriber, drops oldest on overflow so a slow client never wedges ingest. |
| `form4.py` | Form 4 XML parser. Emits one `insider_txn` row per transaction; tags `is_buy=1` only on open-market purchases (code P, non-derivative, shares > 0). |
| `form13f.py` | Form 13F-HR INFORMATION TABLE parser. Emits one `fund_holding` row per `<infoTable>` entry; extracts `periodOfReport` and `filingManager` from the primary doc. |
| `filings13d.py` | SC 13D / 13G regex extractor: percent of class, shares owned, issuer name. |
| `filings8k.py` | 8-K filter — scores filings by reported items (1.01, 2.01, 8.01) and M&A keywords ("definitive agreement", "merger", etc.). |
| `congress.py` | Congressional PTR fetcher — pulls house-stock-watcher and senate-stock-watcher S3 datasets (used by every consumer Congress tracker), normalises field names, dedupes by transaction id. |
| `signals.py` | Computes ranked feeds over the persisted DB: insider clusters, recent buys, activist stakes, M&A events, fund list / fund holdings / position changes, ticker holders, congress trades, ticker synthesis (now incorporates fund + congress signals), hot leaderboard. |
| `db.py` | SQLite schema + helpers. Tables: `insider_txn`, `activist_stake`, `ma_event`, `fund_filing`, `fund_holding`, `congress_trade`, `ingest_state`. WAL mode. |

**Frontend / data**
| File | Purpose |
|---|---|
| `index.html` | Single-file dashboard UI served by `server.py` at `/`. Six tabs: Insider Clusters, Recent Buys, Activist Stakes, M&A Feed, Ticker Synthesis, Whale Leaderboard. |
| `whales.db` | SQLite store, WAL-mode. Created on first run. |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `whales` service. |
| `.dockerignore` | Excludes `*.db`, `*.log`, `__pycache__` from the Docker build context. |
| `requirements.txt` | Python deps. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## API routes

| Route | Returns |
|---|---|
| `GET /api/hot?days=30&limit=50` | Cross-signal "hot now" ranking — tickers ranked by combined synthesis score across insider buys, activist stakes, M&A 8-Ks, fund holders, and congressional trades. |
| `GET /api/insider-clusters?days=30&min_buyers=3` | Tickers with N+ distinct insider buyers in window. |
| `GET /api/insider-recent?days=7&min_value=100000` | Recent material insider buys above $-threshold. |
| `GET /api/activist-stakes?days=14` | Recent SC 13D/13G filings. |
| `GET /api/ma-feed?days=7&min_score=2.0` | Recent 8-Ks flagged as M&A by item codes + keyword scoring. |
| `GET /api/fund-list?limit=100` | Funds known to the system, sorted by most recent 13F filing. |
| `GET /api/fund-holdings?cik=<CIK>&limit=200` | A fund's latest 13F portfolio (one row per holding line). |
| `GET /api/holding-changes?days=120&type=<new\|exit\|increase\|decrease>&limit=100` | Quarter-over-quarter position changes ranked by absolute $ delta. |
| `GET /api/ticker-holders?ticker=<X>&limit=100` | Funds holding a given ticker (latest filing per fund). |
| `GET /api/congress-trades?days=30&chamber=<House\|Senate>&limit=200` | Recent congressional periodic transaction reports. |
| `GET /api/congress-by-ticker?ticker=<X>` | Congress trades for one ticker. |
| `GET /api/synthesis?ticker=XYZ&days=90` | Composite per-ticker view: insider, activist, M&A, fund holders, congress trades + single ranked synthesis score. |
| `GET /api/whale-leaderboard?days=90` | Most active filers (insiders + activists). |
| `GET /api/stream` | Server-Sent Events — emits `hello` on connect and `ingest` after each pass that finds new filings. 20s keepalive comments. |
| `POST /api/admin/ingest-now` | Trigger an ingest pass synchronously. DEV_MODE only. |
| `POST /api/admin/backfill-tickers` | Backfill `issuer_ticker` on 13D/G and 8-K rows ingested before the CIK→ticker map was loaded. DEV_MODE only. |
| `GET /healthz` | Liveness + table counts + last-ingest state + active SSE subscriber count. |
| `GET /` | Dashboard HTML. |

## Data sources

| API | Purpose |
|---|---|
| `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom`     | Recent Form 4 filings |
| `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&output=atom` | Recent SC 13D activist filings |
| `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13G&output=atom` | Recent SC 13G passive filings |
| `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom`    | Recent 8-K material event filings |
| `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F-HR&output=atom` | Recent 13F-HR fund holdings filings |
| `https://www.sec.gov/Archives/edgar/data/<cik>/<accession_nodash>/<accession>-index.json` | Per-filing document index |
| `https://www.sec.gov/files/company_tickers.json` | Official CIK → ticker map (cached daily) |
| `https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json` | Community-maintained House PTR dataset |
| `https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json` | Community-maintained Senate PTR dataset |

EDGAR caps requests at 10/sec and requires a `User-Agent` with contact info
(see `EDGAR_USER_AGENT` env var).

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth and unlock the `/api/admin/ingest-now` endpoint for local dev. |
| `EDGAR_USER_AGENT` | `narve.ai whale tracker contact@narve.ai` | Override with a real address in production. SEC requires this. |
| `INGEST_INTERVAL_S` | `300` | Seconds between ingest passes. |
| `INGEST_FEED_COUNT` | `40` | Entries pulled per Atom feed per pass. |
| `INGEST_13F_LIMIT` | `5` | Max 13F filings processed per pass (XMLs can be megabytes; over multiple passes the full Atom feed is ingested). |
| `CONGRESS_INTERVAL_S` | `3600` | Seconds between Congress dataset pulls. The S3 buckets refresh roughly daily, so this can be coarse. |
| `DISABLE_INGEST` | unset | Set to `1` to disable the ingest loop (e.g. for read-only replicas). |

## Notes

- Insider clustering currently weights by reporter relation (Officer 3.0,
  Director 2.0, 10% Owner 1.5, Other 1.0). Two officers buying is treated
  more strongly than two outside directors.
- 8-K M&A scoring is intentionally conservative — it surfaces material
  agreements (item 1.01) and completed acquisitions (2.01) plus headline
  keyword hits. False positives are expected; the synthesis view combines
  with insider/activist signals for higher-confidence ranking.
- Schema is denormalised; reads are cheap and the dashboard is happy with
  ~100k rows. If the DB grows past that, partition by year or move the
  cold table out.
- 13F values are stored as reported. Historical filings were
  "thousands of dollars" while more recent filings often report in
  dollar precision — relative ranking within a single filing is always
  correct, but absolute magnitudes across very old filings should be
  taken with a grain of salt. The UI labels the field as "Value (as
  reported)".
- Congress data leans on community-maintained S3 mirrors of the official
  House clerk and Senate eFD systems. If a bucket is unavailable, the
  rest of the dashboard keeps working; only the Congress tab will be
  empty.
- Phase 2 shipped: CIK→ticker enrichment, SSE live stream,
  cross-signal "Hot Now" leaderboard.
- Phase 3a shipped (this version): 13F fund holdings + quarter-over-quarter
  position changes + per-ticker fund holders; Congressional periodic
  transaction reports (House + Senate); synthesis scoring now incorporates
  fund-holder presence and Congress buy/sell net.
- Phase 3b candidates: Bayesian fund-skill scoring with forward-return
  calibration (requires daily-price data via Stooq/Yahoo/Polygon),
  unusual options activity (paid: Polygon, CBOE, unusual_whales), dark
  pool prints, foreign-equivalent filings (UK Companies House substantial-
  shareholder notices), CUSIP→ticker enrichment for 13F holdings.

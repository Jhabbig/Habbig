# Whale Tracker Dashboard — TradFi Insider, Activist, M&A, Fund & Congress Signals

Tracks public filings to surface what finance whales are doing in public
markets:

- **Insider transactions** — SEC Form 4 (officers, directors, 10% owners)
- **Activist stakes** — SEC SC 13D / 13G (>5% beneficial ownership)
- **M&A announcements** — SEC 8-K Items 1.01 / 2.01 with keyword scoring
- **Fund quarterly holdings** — SEC Form 13F-HR (institutional managers >$100M AUM)
- **Congressional trades** — House + Senate periodic transaction reports (STOCK Act)
- **Bayesian filer skill** — Beta(α,β) posterior per filer, labeled by ticker-vs-SPY forward returns over a configurable horizon
- **Unusual options activity** — sweep / large-premium options trades (paid feed)
- **Dark pool prints** — off-exchange block prints (paid feed)

Free SEC + Congress + Stooq sources work out of the box. Paid options-flow
/ dark-pool require an `UNUSUAL_WHALES_API_KEY`; CUSIP→ticker resolution
via OpenFIGI works key-less at lower throughput.

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
| `options_flow.py` | unusual_whales adapter — pulls flow alerts + dark pool prints, normalises to the dashboard schema. No-op without `UNUSUAL_WHALES_API_KEY`. Vendor-swappable: replace the two `fetch_*` functions to switch to Polygon / CBOE / Tradier. |
| `llm_client.py` | Generic local-LLM client speaking the OpenAI-compatible Chat Completions API. Works with Ollama (default), LM Studio, vLLM, llama.cpp server, etc. Forces JSON object output, single-flight semaphore so a slow GPU doesn't get flooded. |
| `llm_extract.py` | Domain extractors: `extract_activist_intent` parses 13D/G "Purpose of Transaction" into structured intent + demands + fund type; `extract_ma_terms` parses 8-K material agreements into target / acquirer / consideration / premium / expected-close. Strips HTML, slices to the relevant section before prompting. |
| `openfigi.py` | CUSIP → ticker resolver via OpenFIGI's batch mapping API. Free without a key (25 req/min, batches of 10); accepts `OPENFIGI_API_KEY` for higher throughput. Resolved entries cached in `cusip_ticker`. |
| `prices.py` | Stooq daily-close fetcher with local SQLite cache. Concurrency-capped, normalises tickers to `<ticker>.us`. Co-fetches SPY as the benchmark. |
| `bayesian.py` | Beta(α,β) posterior + Wilson-score 95% confidence interval. Hand-rolled to avoid pulling scipy. |
| `skill.py` | Outcome labeler + skill leaderboards. For each insider buy/sell, activist filing, and congressional trade older than `SKILL_HORIZON_DAYS`, compares ticker forward return to SPY → win/loss. Aggregates per filer into a posterior. |
| `backtest.py` | First-crossing backtest of the synthesis score. For each ticker, finds the earliest date its synthesis crosses a threshold, buys at the next close, holds N days, computes alpha vs SPY. Emits per-trade rows, summary stats (win rate, mean / total / annualised alpha, Sharpe), and a daily equity curve for plotting. |
| `signals.py` | Computes ranked feeds over the persisted DB: insider clusters (now annotated with each buyer's skill posterior), recent buys, activist stakes, M&A events, fund list / fund holdings / position changes, ticker holders, congress trades, ticker synthesis (now incorporates fund + congress signals), hot leaderboard. |
| `db.py` | SQLite schema + helpers. Tables: `insider_txn`, `activist_stake`, `ma_event`, `fund_filing`, `fund_holding`, `congress_trade`, `price_daily`, `filer_outcome`, `ingest_state`. WAL mode. |

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
| `GET /api/options-flow?days=&side=<CALL\|PUT>&min_premium=&limit=` | Unusual options activity alerts (paid feed). |
| `GET /api/options-by-ticker?ticker=<X>&days=` | Options flow for one ticker. |
| `GET /api/dark-pool?days=&min_premium=&limit=` | Dark pool prints (paid feed). |
| `GET /api/dark-pool-by-ticker?ticker=<X>&days=` | Dark pool prints for one ticker. |
| `GET /api/backtest?threshold=&hold_days=&start_date=&end_date=&window_days=` | First-crossing backtest of the synthesis score. Returns `{trades, summary, equity_curve}` with per-trade alpha vs SPY, win rate, mean/median/total/annualised alpha, Sharpe, best/worst trade, and a daily equity curve. |
| `POST /api/admin/llm-extract?target=<activist\|ma\|both>&limit=` | Run a local-LLM extraction pass over pending 13D/G and/or 8-K filings. DEV_MODE only. |
| `POST /api/admin/resolve-cusips?limit=` | Resolve unresolved 13F CUSIPs via OpenFIGI; backfills `fund_holding.issuer_ticker`. DEV_MODE only. |
| `GET /api/skill-leaderboard?filer_type=<insider\|activist\|congress>&min_n=5&horizon_days=30&limit=50` | Bayesian skill leaderboard — posterior mean + 95% Wilson CI per filer, ranked high-confidence first. |
| `GET /api/skill-detail?filer_type=<...>&filer_id=<X>&horizon_days=30` | Per-filer skill posterior + last N labeled outcomes. |
| `POST /api/admin/skill-recompute?filer_type=<insider\|activist\|congress\|fund>` | Trigger a skill-labeling pass. DEV_MODE only. |
| `POST /api/admin/backfill?forms=4,SC 13D&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&max_per_form=500` | Historical EDGAR full-text-search backfill. Warms the skill model on day one. DEV_MODE only. |
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
| `https://stooq.com/q/d/l/?s=<ticker>.us&i=d` | Daily-close OHLCV for US tickers (free, no key). Used to label forward returns for the skill model. |

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
| `SKILL_INTERVAL_S` | `1800` | Seconds between Bayesian skill labeling passes. |
| `SKILL_PER_PASS` | `200` | Max outcomes labeled per skill pass. |
| `SKILL_HORIZON_DAYS` | `30` | Forward-return horizon for win/loss labeling. |
| `PRICES_USER_AGENT` | (default UA) | User-Agent header when fetching price data from Stooq. |
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
- Phase 3a shipped: 13F fund holdings + quarter-over-quarter position
  changes + per-ticker fund holders; Congressional periodic transaction
  reports (House + Senate).
- Phase 3b shipped: Bayesian filer-skill posterior over insider /
  activist / congress filings, labeled by ticker-vs-SPY forward returns
  from Stooq. `Skill` tab + per-buyer skill badges on Insider Clusters.
- Phase 4 shipped:
  - 13F fund-skill: issuer-name → ticker resolution at ingest (and a
    backfill for already-stored holdings). `fund` filer type in the
    skill model. Outcomes derived from quarter-over-quarter position
    changes (new = buy, exit = sell).
  - Historical EDGAR backfill via the full-text search API
    (`efts.sec.gov`) — admin endpoint pulls N months of filings in one
    shot so skill posteriors converge immediately rather than over 3
    months of live ingest.
- Phase 5 shipped:
  - Unusual options activity + dark pool prints (unusual_whales adapter,
    swappable to Polygon / CBOE / Tradier by replacing the two `fetch_*`
    functions in `options_flow.py`). `Options Flow` and `Dark Pool` tabs
    in the UI; synthesis score now incorporates call/put skew + dark
    pool premium.
  - OpenFIGI CUSIP → ticker resolution. 13F holdings now check the
    CUSIP cache first, fall back to issuer-name match, and a background
    pass sends unresolved CUSIPs to OpenFIGI and backfills tickers into
    `fund_holding`. Lifts 13F ticker coverage from ~70% (big-cap names)
    to ~95%+.
- Phase 6 shipped (this version):
  - Synthesis backtest engine + `Backtest` tab (commit f958f5f).
  - Local-LLM extraction layer for 13D/G activist intent + 8-K M&A deal
    terms. Talks any OpenAI-compatible local server (Ollama, LM Studio,
    vLLM, llama.cpp). Default model qwen2.5:7b-instruct runs on a laptop
    GPU and emits structured JSON. Background pass every 10 minutes;
    `POST /api/admin/llm-extract` for manual runs. UI: Activist tab
    shows extracted intent pill + summary; M&A Feed shows target ←
    acquirer, deal type, consideration, expected close, implied premium.
- Phase 7 candidates: foreign-equivalent filings (UK Companies House
  PSC notices, EU Transparency Directive 5% notifications); WebSocket
  push from unusual_whales for true real-time alerting; volume-weighted
  fund-skill (today's binary win/loss treats a $1B and a $1M position
  equally).

# Central Bank Dashboard

Tracks how the world's central banks are moving — policy rates, decision
calendar, market-implied rate path, Polymarket mispricings, and statement
stance — all in one dashboard. Differentiates from `world-state-dashboard` by
being **financial, time-series, and Polymarket-overlaid** (no map, no general
news feed).

Port: **7060**.

## What's built

| Version | View | Data source |
|---|---|---|
| v0   | Policy-rate history chart + latest-readings table for **Fed (DFF)**, **ECB Deposit (ECBDFR)**, **BoE Bank Rate (BOEBR)** | FRED CSV (no key) |
| v0.1 | **Decision calendar** — next 90 days of FOMC / ECB / BoE meetings with imminent/soon/later badges | Hand-curated 2026 dates |
| v0.2 | **Market-implied next-FOMC move** — current rate, implied post-rate, delta in bps, probability bar (cut25 / hold / hike25) | Yahoo Finance ZQ futures + CME-style math |
| v0.3 | **Statement stance ladder** — hawkish ↔ dovish per CB based on rule-based scoring of the latest press release, with matched phrases shown inline | Fed / ECB / BoE RSS feeds + body-text fetch |
| v0.4 | **Polymarket edge** — table of FOMC markets with edge = implied − Polymarket price, sorted by |edge|, BUY YES / SELL YES signals at ±3 pp | Polymarket Gamma API |

All views graceful-degrade when their data source is unreachable (the panel
shows an inline error; other panels keep working).

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/rates` | 6 h | Cached FRED policy rates |
| `GET /api/calendar?horizon_days=90` | — | Upcoming CB meetings |
| `GET /api/implied?force=…` | 30 min | Next-FOMC implied move + probabilities |
| `GET /api/edge` | 5 min (markets) | Polymarket FOMC markets ranked by mispricing vs implied |
| `GET /api/stance` | 1 h | Stance ladder per CB |
| `GET /healthz` | — | Liveness probe |

## Run locally

```bash
cd centralbank-dashboard
cp .env.example .env       # DEV_MODE=1 lets you skip gateway auth
pip install -r requirements.txt
python3 server.py
# http://localhost:7060
```

Or via Docker from the repo root:

```bash
docker compose up --build centralbank
```

Smoke-test individual modules:

```bash
python3 -m ingestion.fred_client          # FRED policy rates
python3 -m ingestion.decision_calendar    # 2026 meeting dates
python3 -m ingestion.implied_path         # ZQ futures + implied move
python3 -m ingestion.polymarket_client    # classifier on canned questions
python3 -m ingestion.cb_statements        # CB RSS pulls
python3 -m analysis.stance_scorer         # scorer fixtures
python3 -m analysis.stance                # full stance ladder
python3 -m analysis.edge                  # full edge view
```

## Files

```
centralbank-dashboard/
├── server.py                       FastAPI + gateway-SSO middleware + 6 routes
├── ingestion/
│   ├── fred_client.py              Policy-rate CSV pull (Fed / ECB / BoE)
│   ├── decision_calendar.py        Hand-curated 2026 FOMC/ECB/BoE meetings
│   ├── implied_path.py             ZQ futures + CME-style implied-rate math
│   ├── polymarket_client.py        Gamma API fetch + rule-based outcome classifier
│   └── cb_statements.py            RSS feeds + HTML body fetcher (Fed/ECB/BoE)
├── analysis/
│   ├── stance_keywords.py          Hawkish/dovish phrase dictionary (extend here)
│   ├── stance_scorer.py            Phrase-match scorer with sentence normalization
│   ├── stance.py                   Composes scraper + scorer into the ladder API
│   └── edge.py                     Joins implied probs + Polymarket prices into edge
├── index.html                      Single-file UI: SVG chart + 4 panels, no JS deps
├── Dockerfile                      Python 3.12-slim, non-root, port 7060
├── requirements.txt                fastapi, uvicorn, defusedxml
├── .env.example
└── README.md                       (this file)
```

## How each piece works

### v0.2 — implied move math

For the contract whose month immediately **follows** the FOMC meeting (so the
contract trades entirely at the post-decision rate):

    implied_post_rate = 100 − contract_price

This avoids the messy intra-month weighting trap when the FOMC falls late in
its own month. Then the implied delta is bucketed across 25-bp steps with
linear interpolation:

    delta = -0.10  →  hold 60%, cut25 40%
    delta = -0.30  →  cut25 80%, cut50 20%

This is the same heuristic the public CME FedWatch tool uses. For trading,
validate against CME's own numbers — there are edge cases (multiple FOMCs in
a quarter, contract roll near decision day) where the simple inversion
deviates.

### v0.3 — stance scoring

Each CB's RSS feed is fetched, the latest monetary-policy item is filtered by
title keyword, and the linked HTML page is fetched and stripped to plain
text. The scorer counts occurrences of phrases from `stance_keywords.py`,
sums weighted counts, and normalizes by sentence count:

    score_norm = Σ (weight × count) / sentence_count

Buckets: ≥ +0.3 HAWKISH, ≤ −0.3 DOVISH, else NEUTRAL. **Matched phrases are
exposed in the API response and rendered as chips in the UI** so you can
sanity-check what triggered the score. That transparency is the entire point
of going rule-based.

The dictionary today skews Fed/BoE-flavored — ECB uses distinct stock
phrases ("transmission of monetary policy", "underlying inflation pressures",
etc.) that aren't covered yet. Adding ECB-specific phrases is a one-file
edit. **Editing `stance_keywords.py` is the supported way to tune behavior.**

### v0.4 — Polymarket edge

The Gamma API is queried for all active markets with end-date in
`[meeting, meeting+7d]`. Each result is keyword-filtered to confirm it's an
FOMC market (must mention Fed/FOMC/Federal Reserve **and** a rate-action
term). The classifier then maps the question text to the same bucket
vocabulary v0.2 produces (`cut25`, `hold`, `hike25`, …) using regex over
verb + bps. Edge:

    edge = implied_prob − polymarket_yes_price

Threshold for surfacing a BUY YES / SELL YES signal: ±3 pp absolute.
Polymarket's own bid-ask plus our modelling slack live below that.

## Roadmap

| Step | Status | Adds |
|---|---|---|
| v0   | ✓ done | FRED policy-rate ingestion + chart |
| v0.1 | ✓ done | Decision calendar |
| v0.2 | ✓ done | Implied next-FOMC move from ZQ futures |
| v0.3 | ✓ done | Statement scraper + stance scorer + ladder |
| v0.4 | ✓ done | Polymarket edge table |
| v0.5 | open  | Statement diff viewer (compare two press releases side-by-side) |
| v0.6 | open  | Extend implied path to ECB (€STR OIS) and BoE (SONIA OIS) |
| v0.7 | open  | Auto-scrape annual CB calendar pages so meeting dates refresh |
| v0.8 | open  | ECB-specific phrases in `stance_keywords.py` (current dictionary skews Fed/BoE) |
| v0.9 | open  | BoJ — needs direct BoJ stats API (FRED proxies are noisy) |
| v1.0 | open  | Wire into `gateway/config.json` for subdomain routing once subscription model is decided |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally. |
| `PORT` | `7060` | Override listen port. |

## Caveats / known limits

- **Not investment advice.** The edge table flags mispricings; it does not
  account for Polymarket's bid-ask, on-chain gas, or settlement risk. Validate
  every signal against CME's own FedWatch numbers and inspect the Polymarket
  order book before trading.
- **Decision calendar dates are hand-curated for 2026.** They will need to be
  refreshed annually until v0.7 (auto-scrape) lands. The file has loud
  comments and points to each CB's official source.
- **Stance dictionary is conservative and Fed/BoE-flavored.** ECB statements
  often score NEUTRAL because their stock phrases aren't in the dictionary
  yet. Adding them is a one-file edit (`analysis/stance_keywords.py`).
- **BoJ is not covered.** FRED's BoJ proxies (discount rate, overnight call
  rate) are noisy; doing it right needs the BoJ stats API directly. Roadmap
  item v0.9.
- **Yahoo Finance can rate-limit** the ZQ contract pull. The 30-minute cache
  insulates against this in normal use, but if Yahoo blocks the User-Agent,
  the implied panel falls back to "missing futures price" rather than crashing.

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
| v0.5 | **Cross-venue arbitrage + Trade buttons** — Kalshi YES price beside Polymarket YES on the same FOMC outcome. `Arb (P−K)` column flags spreads >3 pp. One-click **Trade Poly →** and **Trade Kalshi →** deep-links so users place orders with their own accounts on each venue. | Kalshi Trade API v2 (read-only public endpoint) |
| v0.6 | **Full OIS curve overlay** — extends `implied_path` from "next FOMC" to a full 18-month strip of monthly Fed Funds futures. Each month shows the implied average FF rate; the curve is overlaid on the rate-path chart as a dashed teal forward line, anchored at the latest spot reading. | Yahoo Finance ZQ contract chain (continuous) |
| v0.7 | **Macro release tracker** — Headline / Core CPI, Headline / Core PCE, NFP. Latest YoY % (or MoM jobs change for NFP), 24-month sparkline, days-until-next-release with imminent / soon / later badges. Release dates computed from BLS / BEA conventions, no scraping. | FRED CSV |
| v0.8 | **Phase 2 — In-app Kalshi trading** — per-user encrypted Kalshi API key storage (Fernet), RSA-PSS request signing, order modal with paper-mode default + explicit confirm-each-order, balance + positions snapshot, append-only audit log. The "Trade Kalshi →" button now places real orders through the dashboard instead of deep-linking out. | Kalshi Trade API v2 (private, signed) |

All views graceful-degrade when their data source is unreachable (the panel
shows an inline error; other panels keep working).

## Trading model

| Venue | UX | Auth | Risk surface |
|---|---|---|---|
| **Polymarket** | Deep-link out (new tab) | User's Polymarket account in browser | None on our side — we never touch Polymarket creds |
| **Kalshi** | **In-app order modal** with paper / prod toggle and confirm-per-order | RSA-PSS signed requests with the user's own Kalshi API key + private key (PEM), encrypted at rest with Fernet | Custodial of an encrypted private key per user; see security notes below |

### How Kalshi trading works in the dashboard

1. User visits the Kalshi trading panel (bottom of the page) and pastes their
   API key id + RSA private key. Mode defaults to **PAPER** (Kalshi's demo venue,
   fake money).
2. The dashboard encrypts both fields with Fernet (AES-128-CBC + HMAC-SHA-256,
   authenticated) using the operator's `CB_KEY_STORE_SECRET` env var, and
   stores the ciphertext in `data/key_store.db` (SQLite).
3. When the user clicks **Trade Kalshi →** in the edge table, an in-dashboard
   modal opens with the ticker, side (YES/NO), action (BUY/SELL), count, and
   limit price (¢) pre-filled. The mode banner is colored red for PROD and
   blue for PAPER.
4. On submit, the server decrypts the credentials *for the duration of one
   signing call*, builds the RSA-PSS-signed request, hits Kalshi, and writes
   an audit-log entry with the request payload (private key redacted) and
   Kalshi's response.
5. Switching from PAPER to PROD requires an explicit `confirm_real_money` flag
   plus a `confirm()` dialog. Every order requires `confirm: true` in the
   request body.

### Security notes

- **The private key never leaves the operator's host.** It's encrypted at
  rest with a master key the *operator* supplies via `CB_KEY_STORE_SECRET`.
  Without that env var, the SQLite ciphertexts are useless.
- **Plaintext private-key material is held in memory for one signing call.**
  We re-decrypt on every request rather than caching a decrypted copy.
- **Audit log redacts known-secret fields** (`private_key_pem`, `signature`,
  etc.) before writing. Greppable as `<redacted>`.
- **Paper mode by default.** Real-money mode requires an explicit toggle
  with a `confirm()` warning.
- **Every order is a user click.** No auto-trading, no signal-driven fires.
- **No order modification (yet).** Place + cancel only; v0.9 will add resize
  and price-replacement.

### Operator setup

1. **Generate the master key for at-rest encryption** (one-time, store securely):

   ```bash
   python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

2. **Set it in the environment** (e.g. via `gateway/.env.production`):

   ```
   CB_KEY_STORE_SECRET=<the urlsafe-base64 string above>
   ```

   In `DEV_MODE=1` a random key is generated automatically and stashed at
   `data/dev_master.key` — never use that for production.

3. **Restart the dashboard.** Users can now configure their Kalshi creds via
   the trading panel.

### What's still Phase 2.1 (not in v0.8)

- **Order modification / resize** — currently place + cancel only.
- **Multi-key per user** — one set of Kalshi creds per `user_id`.
- **Webhook-driven fill notifications** — currently the user reloads the panel
  to see balance / position changes.
- **Cross-dashboard credential sharing** — credentials live in this dashboard's
  SQLite; if you want sports-dashboard to also trade Kalshi, you'd duplicate
  the storage or extract `trading/` to a shared service.

## Endpoints

| Path | Cache | Purpose |
|---|---|---|
| `GET /` | — | Dashboard UI |
| `GET /api/rates` | 6 h | Cached FRED policy rates |
| `GET /api/calendar?horizon_days=90` | — | Upcoming CB meetings |
| `GET /api/implied?force=…` | 30 min | Next-FOMC implied move + probabilities |
| `GET /api/edge` | 5 min (markets) | Cross-venue table: bucket × {Poly, Kalshi, Implied} + edges + arb spreads + trade-out URLs |
| `GET /api/kalshi` | 5 min | Raw Kalshi FOMC market list (debug-friendly) |
| `GET /api/stance` | 1 h | Stance ladder per CB |
| `GET /api/ois?months_ahead=18` | 30 min | Full Fed Funds futures strip → implied avg FF rate per month |
| `GET /api/econ` | 6 h | CPI / Core CPI / PCE / Core PCE / NFP latest + sparkline + next-release date |
| `GET /api/keys/kalshi/status` | — | Whether the calling user has credentials configured + their mode (paper/prod) |
| `POST /api/keys/kalshi` | — | Save / replace credentials (encrypted at rest) |
| `DELETE /api/keys/kalshi` | — | Remove credentials |
| `POST /api/keys/kalshi/mode` | — | Toggle paper ↔ prod (prod requires `confirm_real_money: true`) |
| `GET /api/portfolio/balance` | — | Authenticated Kalshi balance |
| `GET /api/portfolio/positions` | — | Authenticated Kalshi positions |
| `GET /api/orders` | — | Authenticated Kalshi open orders |
| `POST /api/order/kalshi` | — | Place a limit order (requires `confirm: true`) |
| `DELETE /api/order/kalshi/{id}` | — | Cancel an open order |
| `GET /api/audit?limit=100` | — | This user's trading-action log (newest first) |
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
python3 -m ingestion.outcome_classifier   # 15 fixtures across Poly + Kalshi phrasings
python3 -m ingestion.polymarket_client    # Polymarket FOMC market fetch
python3 -m ingestion.kalshi_client        # live Kalshi FOMC fetch
python3 -m ingestion.ois_curve            # 18-month Fed Funds futures strip
python3 -m ingestion.econ_releases        # CPI/PCE/NFP latest + release calendar
python3 -m ingestion.cb_statements        # CB RSS pulls
python3 -m analysis.stance_scorer         # scorer fixtures
python3 -m analysis.stance                # full stance ladder
python3 -m analysis.edge                  # full cross-venue edge view
```

## Files

```
centralbank-dashboard/
├── server.py                       FastAPI + gateway-SSO middleware + 7 routes
├── ingestion/
│   ├── fred_client.py              Policy-rate CSV pull (Fed / ECB / BoE)
│   ├── decision_calendar.py        Hand-curated 2026 FOMC/ECB/BoE meetings
│   ├── implied_path.py             ZQ futures + CME-style implied-rate math
│   ├── outcome_classifier.py       Shared rule-based bucket classifier (Poly + Kalshi)
│   ├── polymarket_client.py        Gamma API fetch; delegates to outcome_classifier
│   ├── kalshi_client.py            Kalshi /trade-api/v2 public read; deep-link builder
│   ├── ois_curve.py                Full 18-month FF futures strip → monthly avg rates
│   ├── econ_releases.py            CPI / Core CPI / PCE / Core PCE / NFP via FRED + release dates
│   └── cb_statements.py            RSS feeds + HTML body fetcher (Fed/ECB/BoE)
├── trading/
│   ├── kalshi_auth.py              RSA-PSS signed-request builder
│   ├── key_store.py                Per-user encrypted Kalshi creds (Fernet + SQLite)
│   ├── audit.py                    Append-only JSONL audit log of every trading action
│   └── order_manager.py            place / cancel / balance / positions / list-orders
├── analysis/
│   ├── stance_keywords.py          Hawkish/dovish phrase dictionary (extend here)
│   ├── stance_scorer.py            Phrase-match scorer with sentence normalization
│   ├── stance.py                   Composes scraper + scorer into the ladder API
│   └── edge.py                     Cross-venue join: Implied × Polymarket × Kalshi → edges + arb
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
| v0.5 | ✓ done | Kalshi cross-venue arbitrage panel + Trade-on-Poly/Kalshi deep-links |
| v0.6 | ✓ done | Full OIS curve overlay (18-month FF futures strip on rate-path chart) |
| v0.7 | ✓ done | Macro release tracker (CPI / Core CPI / PCE / Core PCE / NFP) — latest, sparkline, next-release date |
| v0.8 | ✓ done | **Phase 2 — In-app Kalshi trading** (encrypted creds + RSA-PSS + paper-mode default + confirm-per-order + balance/positions + audit log) |
| v0.9  | open  | Phase 2.1 — order modification (resize, price replace) + webhook-driven fill notifications |
| v0.10 | open  | Consensus-vs-actual surprise tracking (paid feeds — defer until users justify) |
| v0.11 | open  | Statement diff viewer (compare two press releases side-by-side) |
| v1.0  | open  | Extend implied path to ECB (€STR OIS) and BoE (SONIA OIS) |
| v1.1  | open  | Auto-scrape annual CB calendar pages so meeting dates refresh |
| v1.2  | open  | ECB-specific phrases in `stance_keywords.py` (current dictionary skews Fed/BoE) |
| v1.3  | open  | BoJ — needs direct BoJ stats API (FRED proxies are noisy) |

## Env vars

| Var | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required behind the gateway. |
| `DEV_MODE` | unset | Set `1` to bypass gateway auth locally; also auto-generates a master key for the trading store at `data/dev_master.key`. |
| `BIND_HOST` | `0.0.0.0` | Listen address. Set `127.0.0.1` for localhost-only. |
| `PORT` | `7060` | Override listen port. |
| `CB_KEY_STORE_SECRET` | required for trading | Fernet master key (urlsafe-base64) used to encrypt user Kalshi credentials at rest. Generate with `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`. Without this, all trading endpoints return an error in non-DEV mode. |

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

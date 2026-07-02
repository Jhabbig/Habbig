# narve.ai — Product Balance Sheet

*As of July 2, 2026 (commit `13e3272`). A statement of what the repo owns versus what it owes — engineering assets against engineering/operational liabilities. Figures verified against the codebase; monetary amounts are configured prices, not audited revenue.*

---

## ASSETS

### Current assets — live and revenue-capable

| Asset | Book value | Basis |
|---|---|---|
| **Gateway platform** (`gateway/`) | ~10,600 LOC | Auth, invite-gated signup, Stripe billing (checkout + webhook), reverse proxy, admin + fleet ops panel, SSE/Redis fan-out, encrypted trading-credential vault, order routing (Polymarket/Kalshi/Alpaca). The single control plane every product depends on. |
| **Market Edge — Stocks & Crypto** (`crypto-dashboard/` + `stock-dashboard/`) | ~22,100 LOC · $9.99/mo, $99/yr | Two ML signal apps under one subscription (`access_alias`). Live Stripe price IDs configured. CryptoEdge 5-min direction ensembles on 1-second Binance data; StockSignal daily XGBoost stack on 5y yfinance data. |
| **Weather, Climate & Disasters** (`polymarket_weather_dashboard/`) | ~11,500 LOC · $7.99/mo, $79/yr | 8-model NWP ensemble consensus vs. Polymarket/Kalshi prices; absorbed the Disasters + Climate products as tabs (with `legacy_grants` honoring old subscribers). Live Stripe prices. |
| **Midterm Predictor** (`midterm-dashboard/`) | ~20,000 LOC (incl. React) · $14.99/mo, $149/yr | Six-source election aggregation with Brier-weighted ensemble, divergence detection, embeds. Live Stripe prices. |
| **Trading bots** (`polymarket-bot/`, `polymarket_weather_bot/`) | ~3,000 LOC | Paper-trading engines whose track records are subscriber-facing content; weather bot is live-trading capable (Kelly-sized, FOK→GTC retry logic). |

**Maximum configured storefront run-rate:** $32.97/mo per fully-subscribed user across the three listed products. Bundles hardcoded in `server.py` (`BUNDLE_PLANS`): "Trader" $49/mo / $399/yr, "Pro" $149/mo / $1,199/yr.

### Long-term assets — built, parked, revivable

| Asset | Book value | Basis |
|---|---|---|
| **9 parked dashboards** (sports, world, top-traders, centralbank, truth, ai-race, crypto-trackers, religion, whale) | ~68,000 LOC | Delisted but preserved: revival is documented as two config flags + one systemd unit. Sports/world/top-traders retain live Stripe price IDs ($19.99 / $5.99 / $12.99 per month); parked-subdomain visits are tallied on `/admin/fleet` as demand signals. |
| **financial-matrix-toolkit** | ~5,900 LOC + 13 pytest suites | 20 hand-rolled matrix models with leak-guarded, significance-tested, walk-forward evaluation. Research IP / future analytical engine; not wired to any product yet. |
| **Investor key system** (superuser keys) | in gateway | Expiring, aspect-scoped, no-account access for demos and investor relations, with admin UI and audit logging. Business-development infrastructure. |
| **Legacy prototypes** (trading-dashboard, voters-dashboard, climate/disasters standalone) | ~19,300 LOC | Salvage value only: superseded (trading → stock-dashboard), orphaned (voters — never registered in the gateway), or merged (climate/disasters). |

### Intangible assets

- **Data-pipeline integrations:** 40+ external sources wired and normalized — Polymarket (Gamma/CLOB/Data), Kalshi, Binance, yfinance, Open-Meteo, NWS/METAR, NOAA/NASA/USGS/FEMA/NSIDC, SEC EDGAR, CFTC, FRED, Census/BEA/BLS/FEC, The Odds API, Anthropic. Nearly all keyless/free-tier (36 env keys cover the full surface).
- **Ops discipline:** deploy-triggered snapshots with safe SQLite `.backup`, hardened systemd sandboxing, CI lint gate, Cloudflare Tunnel + Tailscale ingress.
- **Test estate:** 80 `test_*.py` files repo-wide.
- **Brand/domain:** narve.ai, wired end-to-end (DNS scripts, subdomain catalog, PWA manifests).
- **Methodology reputation (internal):** the toolkit's "honest evaluation" standards — nulls, HAC significance, leak audits — are a house style worth more than any single model.

---

## LIABILITIES

### Current liabilities — costing or risking something *now*

| Liability | Exposure | Basis |
|---|---|---|
| **Stale CI deploy workflow** | Deploy failure + secret data-loss | `.github/workflows/deploy.yml` still syncs/restarts parked services (restart loop fails post-trim) and its rsync `--delete` excludes `.env` but not `.env.production` — the exact "lost ODDS_API_KEY twice" incident `deploy.sh` was fixed to prevent. |
| **Stripe renewal gap** | Billing customers without service | Webhook handles `checkout.session.completed` but not `invoice.paid`; subscriptions expire locally at a fixed 30 days while Stripe keeps charging. |
| **Superuser keys in plaintext** | Credential leak | Stored unhashed in SQLite, accepted as URL query params (logs/history/referrer leakage); docs claim constant-time compare + caching that the code doesn't implement. |
| **Single-box, single-file blast radius** | Total-outage / total-leak risk | One Ubuntu server; every service loads the same `gateway/.env.production` (all secrets, one file). |
| **Email relay `relay.py`** | Remote-execution surface | Executes `claude -p <email body>` headlessly, authorized only by a spoofable From-header allowlist. |
| **Broken subscriber-facing page** | Product quality | `polymarket-bot` writes `poly_trades.json` to its own dir; crypto server reads it from *its* dir — `/polybot` shows the bot offline unless deployment symlinks the file. |

### Long-term liabilities — technical debt

- **Dead/unwired code sold as features:** stock Tier-1 modules (Kelly sizing, Greeks, Alpaca broker) advertised in the product description but never imported; weather bot's daily-loss circuit breaker (`record_pnl`) never called; ~280 dead lines in `ml_predictor.py`.
- **Duplicate codebases:** disasters/climate ingestion vendored byte-identical into the weather dashboard *and* kept as standalone legacy dirs (~7,000 LOC duplicated).
- **Documentation drift:** root README omits 6 directories (including the newest asset, financial-matrix-toolkit); STRIPE_SETUP.md references code that doesn't exist and prices that disagree with `BUNDLE_PLANS`; brand fragmented across Habbig / narve.ai / betyc / NoRain / StockSignal.
- **Fragile coupling:** `sys.path.insert` imports across directories (polymarket-bot → crypto-dashboard, trading-dashboard → stock-dashboard); cross-app nav by host-string surgery (`'crypto.' → 'stocks.'`); dashboard reads bot's SQLite by relative path.
- **Repo hygiene:** SQLite WAL sidecars committed (`predictions.db-wal`, ~2 MB of leaked scrape data); a 51,527-line repomix snapshot of the whole codebase committed under `.snapshots/`; prod SSH target and Tailscale IPs committed in docs/scripts.
- **CI coverage inversion:** the only test suites CI runs belong to *parked* services (sports, climate); zero CI tests for the 5 live services; lint is `F821`-only.
- **4 parked dashboards have `TODO` Stripe price strings** (centralbank, truth, crypto-trackers, whale) — unparking them without fixing billing would break paid signup.

### Contingent liabilities

- **Real-money paths:** weather bot live mode (wallet `PRIVATE_KEY`, Kalshi RSA), gateway-held user trading credentials (Fernet-encrypted), one-click CLOB orders — financial and regulatory exposure if anything above fails.
- **Resolution-source mismatch:** stock bot resolves bets on yfinance daily closes while Polymarket resolves on Pyth 1-minute candles — a track-record integrity risk.
- **Synthetic-data optics:** all headline numbers in the toolkit README come from seeded synthetic data (disclosed in `data/README.md`, silently labeled `cache` at runtime) — reputational risk if quoted as real-market results.
- **Key-person/bus-factor = 1:** hardcoded `User=julianhabbig`, personal tailnet paths, Mac-dev/one-Ubuntu-box topology.

---

## EQUITY — what the owner nets out

**Owner's equity = a trimmed, coherent 3-product prediction-market storefront** (one gateway, one cookie, one Stripe account, ~67k LOC live) **+ option value on a 9-dashboard parked fleet** (revivable by config flag, demand-metered via `/admin/fleet`) **+ a quant research foundation** whose honest-evaluation methodology is positioned to become the analytical engine behind the paid signals.

**Retained earnings (figurative):** four months of velocity — 140 commits, a same-day-reverted Supabase pivot that settled the architecture on SQLite+WAL, an investor-ready milestone, a disciplined storefront trim, and a finishing sprint of rigorous quant work.

**Auditor's note:** actual revenue is not determinable from the repo — signup is invite-token-gated, `auth.db` is (correctly) not committed, and no subscriber counts exist in git. The largest single value-unlock available is cheap: fix `deploy.yml` (parked-service list + `.env.production` exclusion) and add an `invoice.paid` webhook handler — together they close the two liabilities most likely to cost real money this quarter.

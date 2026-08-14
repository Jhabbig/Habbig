# Bloomberg Parity Plan — $2M Deployment

**Owner:** Julian Habbig · **Budget:** $2,000,000 · **Horizon:** 24 months
**Product:** the whale-tracker dashboard grows into the **narve terminal** —
a professional-grade market terminal with the ownership/skill engine as its moat.

---

## 1. Honest framing: what $2M buys

Bloomberg spends billions a year and its deepest moats (IB chat network,
fixed-income market plumbing, first-party newsroom, execution network,
40 years of point-in-time data) are not purchasable at any startup budget.
What $2M **does** buy, deployed carefully:

| Tier | Description | Cost to reach | Status |
|---|---|---|---|
| 1. Best-in-class ownership vertical | Insider/activist/13F/Congress/options + Bayesian skill + synthesis | ~$0 (done) | ✅ shipped (phases 1–9) |
| 2. Professional terminal (Koyfin/YCharts class) | + real-time US equities & options, point-in-time fundamentals, estimates, news, screening, portfolio | **$2M covers this fully** | ← this plan |
| 3. Bloomberg | + global exchanges, FI analytics, chat network, execution | ~$10M+/yr data licensing alone | not the goal |

The strategy is to **win tier 2 while keeping the tier-1 moat** nobody else
has: calibrated filer-skill and cross-signal synthesis. A Koyfin-class
terminal that also tells you *whose filings historically made money* is a
differentiated product, not a clone.

---

## 2. Budget allocation (24 months, $2.0M)

| Line | 24-mo total | Monthly (avg) | Notes |
|---|---:|---:|---|
| Data licensing | $480k | $20k | See §3 — gated, ramps up from ~$3k/mo |
| Cloud infrastructure | $180k | $7.5k | See §4 |
| Engineering (2 senior contractors) | $600k | $25k | Data-pipeline + frontend; AI (me) does the rest |
| Data-ops / QA contractor (part-time) | $150k | $6k | The unglamorous thing Bloomberg does with thousands of people |
| Legal & licensing counsel | $120k | $5k | Exchange data agreements, redistribution terms, ToS, entity |
| GPU / LLM inference | $70k | $3k | Local extraction at scale; Legion + burst rentals |
| Design & marketing | $100k | $4k | Terminal UI polish, launch |
| **Reserve (15%)** | **$300k** | — | Vendor price shocks, legal surprises |
| **Total** | **$2.0M** | | |

**Gating rule:** no annual vendor contract is signed until the free-data tier
(Phase 10, shipping now) demonstrates 90-day user retention. Revenue from the
existing Stripe gateway (price the terminal at $49–99/mo) should cover the
data line by month ~12 at roughly 250–400 subscribers.

---

## 3. Data vendor plan (the core of the spend)

Selected for price-to-depth and clean redistribution terms. Ranges are
list-price estimates; counsel negotiates redistribution riders.

| Domain | Vendor | Est. cost | What it closes |
|---|---|---:|---|
| Real-time US equities + options | **Polygon.io** (Advanced → institutional) | $200–2,500/mo | The real-time gap. REST + WebSocket; our `market_data.py` adapter is already built for it |
| Point-in-time fundamentals + prices to 1998 | **Nasdaq Data Link — Sharadar** (SF1/SEP/TICKERS) | $500–1,500/mo | Survivorship-bias-free history; upgrades the free XBRL layer |
| News wire | **Benzinga** newsfeed API | $1,000–2,000/mo | Sub-minute headlines; our `news.py` adapter slot is already built |
| Earnings estimates & consensus | **Intrinio** or **Zacks** | $2,000–5,000/mo | The one domain with no good free source |
| Options flow / dark pool | **unusual_whales** (already integrated) | $100–300/mo | Adapter + WS shipped in phases 5/8 |
| Corporate bonds | **FINRA TRACE** dissemination | **free** | A credible bonds MVP at zero data cost — most competitors skip this |
| Treasuries / macro | FRED, Treasury Direct | **free** | Curves, rates |
| SEC filings + XBRL fundamentals | EDGAR / companyfacts | **free** | Shipped (filings phases 1–9; XBRL in phase 10) |
| UK / AU / JP ownership | Companies House, ASX, EDINET | **free** | Shipped (phases 7/9) |
| Exchange display fees (CTA/UTP) | per-user pass-through | ~$3/user/mo nonpro | Charged into the subscription price, not the budget |

Ramp: months 1–3 $0 (free tier proves out) → months 4–9 ~$4k/mo (Polygon +
Sharadar + Benzinga) → months 10–24 ~$25–30k/mo (estimates + institutional
tiers) as revenue arrives.

---

## 4. Infrastructure architecture ($180k line)

**Current:** SQLite (WAL) on one box behind the hobby gateway. Fine to ~10k
users for filings; wrong for tick data.

**Target:**

```
                    ┌────────────────────────────────────────┐
  Polygon WS ──────▶│  Ingest tier (async workers, k8s)      │
  Benzinga  ──────▶│  vendor adapters (hot-swappable)        │
  EDGAR/XBRL ─────▶│  LLM extraction pool (GPU nodes)        │
  TRACE/FRED ─────▶└──────────┬─────────────────────────────┘
                               ▼
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
  ClickHouse            PostgreSQL               Redis
  (ticks, bars,         (filings, fundamentals,  (hot quotes,
   options flow)         profiles, outcomes)      SSE fan-out)
        └──────────────────────┼──────────────────────┘
                               ▼
                    FastAPI API tier (stateless, horizontal)
                               ▼
                    WebSocket/SSE-first terminal UI
```

- **ClickHouse Cloud** (~$1–2k/mo): tick + minute-bar store. SQLite cannot hold Polygon's firehose.
- **Managed Postgres** (~$500/mo): everything currently in `whales.db`. The schema is already column-additive-migratable (phase 9's `_migrate`); the port is mechanical because all access goes through `db.py`.
- **Redis** (~$200/mo): quote cache + SSE fan-out across API replicas.
- **k8s (EKS/GKE)** (~$2–3k/mo all-in): ingest workers separate from API replicas; the `DISABLE_INGEST` flag shipped in phase 2 already anticipates this split.
- **GPU**: the Legion runs steady-state extraction (qwen2.5:7b as shipped); Runpod A100 bursts (~$2/hr) for backfill sweeps over every historical 8-K/13D/DEF 14A and earnings-call transcripts.
- **Observability**: Grafana Cloud (~$300/mo), Sentry (~$100/mo), plus the `/healthz` counts already exposed.

**Reliability targets:** 99.9% API uptime (not five-nines — honest for the team size), RPO 1h (continuous Postgres WAL archiving), RTO 4h.

---

## 5. Build phasing

| Phase | Cost gate | Scope |
|---|---|---|
| **P10 (now, $0)** | none | XBRL fundamentals, news framework, market-data abstraction, screener — **this commit** |
| **P11 (mo 2–4)** | ~$4k/mo | Turn keys: Polygon real-time + WS, Sharadar point-in-time, Benzinga wire; watchlist streaming quotes |
| **P12 (mo 4–8)** | free data | Bonds via TRACE, treasury curves via FRED, portfolio tracker (positions, P&L vs SPY, factor-lite attribution), CSV/Excel export & user API keys |
| **P13 (mo 6–10)** | ~$5k/mo | Estimates & consensus; earnings calendar; transcript LLM summaries; screener backtesting (screen → historical alpha) |
| **P14 (mo 9–15)** | infra | SQLite→Postgres port, ClickHouse tick store, k8s split, multi-replica SSE via Redis |
| **P15 (mo 12–24)** | scale | Global equities (LSE/TSX/EU via Polygon-partner feeds), mobile UI, enterprise SSO, SOC 2 |

**Deliberately out of scope** (the moats we don't attack): chat network,
execution/EMSX, first-party newsroom, YAS-depth fixed-income analytics,
global exchange tick licensing.

---

## 6. Revenue math (why $2M is enough)

- Terminal at **$79/mo** (between Koyfin Pro $79 and Unusual Whales $48+; Bloomberg is $2k+/mo).
- Data + infra steady-state ≈ $30k/mo at full ramp → **~380 subscribers to break even on COGS**; 1,000 subs = ~$950k ARR against ~$400k/yr costs.
- The gateway already has Stripe subscriptions per dashboard — zero payments work needed.
- The wedge audience: the Unusual Whales / Quiver market (retail-pro who pay for edge), upsold on "the only terminal with calibrated filer skill."

## 7. Risk register

| Risk | Mitigation |
|---|---|
| Vendor repricing (Polygon/Sharadar hikes) | Adapter layer makes every vendor swappable; 15% reserve |
| Exchange licensing complexity | Counsel line budgeted; nonpro-only at launch keeps fees ~$3/user |
| Data quality (community Congress feeds) | P13 moves Congress to first-party clerk/eFD scraping with the QA contractor |
| Key-person (solo founder) | Everything self-hosted + documented per-module READMEs; 2 contractors cross-trained |
| LLM extraction errors reaching users | Confidence fields shipped in phase 6b; UI labels extracted vs. reported data |

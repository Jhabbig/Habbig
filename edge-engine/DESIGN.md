# Edge Engine — Architecture Design

**Status:** Design (approved architecture: *Standalone Edge Engine*)
**Scope:** A new `edge-engine/` service that ingests every dashboard's signals into
one canonical ledger, paper-trades them through the existing risk machinery,
resolves outcomes, scores each signal source honestly, and gates promotion to
live — docking onto the gateway so going live later is incremental.

---

## 1. Why this exists (the thesis)

The suite already *produces* signals in ten places (crypto/stock direction,
weather/sports/election edges, insider flows, cross-venue arb). What it does
**not** have is one honest, cross-domain answer to: *which of these signals
actually make money after costs, and which just look clever?*

The Financial Matrix Toolkit already encodes the right discipline for a single
domain — every model is scored **out-of-sample against a NULL benchmark**,
direction is judged **after transaction costs**, and probabilities are
**calibrated** before they size anything (`financial-matrix-toolkit/README.md:11`,
`core.py:104`, `harness.py:134`, `calibration.py`). The Edge Engine
**generalises that discipline to every surface**: log every signal, resolve its
real outcome, paper-trade it, and only "promote" a source to live once its paper
track record is statistically significant after costs and well-calibrated.

The engine's north star is a **track record you can trust**, not a
prediction firehose.

---

## 2. Goals / non-goals

**Goals**
- One **canonical ledger** of every signal, its paper position, its resolved
  outcome, and its running score — dual-backend (SQLite dev / Postgres prod),
  reusing the gateway's DB idioms.
- **Pluggable resolvers** that reuse each domain's *existing* resolution logic
  rather than reinventing it.
- A **paper executor** that *wraps the existing risk engine* (sizing, stops,
  heat) and the existing execution contract — so the live switch is a one-line
  branch, not a rewrite.
- An honest **promotion gate**: paper → candidate → promoted, driven by
  significance-after-costs + calibration + expectancy, reusing FMT's stats.
- A **track-record dashboard** surfacing per-source skill, calibration, equity
  curve, and promotion status.
- **Incremental live- readiness**: the same ledger and executor that run paper
  today run live tomorrow behind the gate.

**Non-goals (for v1)**
- Not an autonomous live-trading button. Promotion is **human-in-the-loop**;
  the gate produces a *recommendation*, an operator flips the live flag.
- Not a replacement for each dashboard's own UI or its own logic — the engine
  *consumes* signals, it does not compute them.
- Not a home for signals with no resolvable outcome (world-state indices,
  crypto-trackers live conditions) until an outcome is bound to them (§9).

---

## 3. Architecture at a glance (why "A")

A **standalone service** (`edge-engine/`, its own port, docked onto the gateway
like every other dashboard) owning a **canonical ledger DB**, with **pluggable
resolvers** and a **paper executor** that wraps the existing risk engine, a
**promotion gate**, and a **track-record dashboard**. Surfaces emit signals via
a thin client.

```
                         ┌──────────────────────────────────────────────┐
   dashboards / bots     │                 EDGE ENGINE                    │
   (signal producers)    │                                                │
  ┌───────────────┐      │   ┌──────────┐   ┌───────────────┐            │
  │ crypto        │─emit─┼──▶│ ingest   │──▶│ canonical      │            │
  │ stock         │ thin │   │ /api/    │   │ ledger (DB)    │◀────┐      │
  │ weather (bot) │client│   │ signals  │   │ edge_signals   │     │      │
  │ sports        │      │   └──────────┘   │ edge_positions │     │      │
  │ midterm       │      │                  │ edge_resolutions│    │      │
  │ polymarket-bot│      │   ┌──────────┐   │ edge_scores    │     │      │
  └───────────────┘      │   │ paper    │──▶│ edge_source_   │     │      │
                         │   │ executor │   │   status       │     │      │
                         │   │ (wraps   │   └───────┬────────┘     │      │
                         │   │  risk    │           │              │      │
                         │   │  engine) │   ┌───────▼────────┐     │      │
                         │   └──────────┘   │ resolver loop  │─────┘      │
                         │   ┌──────────┐   │ (per-domain    │            │
                         │   │ mark-to- │   │  plugins)      │            │
                         │   │ market   │   └───────┬────────┘            │
                         │   │ worker   │           │                     │
                         │   └──────────┘   ┌───────▼────────┐            │
                         │                  │ scorer +       │            │
                         │                  │ promotion gate │            │
                         │                  │ (FMT stats)    │            │
                         │                  └───────┬────────┘            │
                         │                          │                     │
                         │                  ┌───────▼────────┐            │
                         │                  │ track-record   │            │
                         │                  │ dashboard      │            │
                         │                  └────────────────┘            │
                         └──────────────────────────────────────────────┘
                                     ▲ docks onto gateway (auth/proxy/gate)
```

**Why A over the alternatives**

- **B (shared library + per-surface DBs + aggregator):** no *canonical* ledger —
  the whole point is one honest cross-domain track record. Aggregating five
  incompatible SQLite schemas after the fact re-imports every domain's quirks
  (weather's `calibration` table is even unwired — `polymarket_weather_bot`).
  A single normalized schema is the deliverable, not a reporting view.
- **C (fold into the gateway):** the gateway already runs `workers=1` because
  rate-limit/CSRF state is in-process (`gateway/server.py:5051`), and it is a
  5,000-line auth/proxy surface. Adding scan loops, a resolver worker, and a
  paper executor into that process couples reliability of billing/auth to the
  reliability of trading loops. Keep them separate; dock via config.
- **A (standalone):** isolates the trading/scoring loops, gets its own deploy +
  CI + health probe, reuses the gateway only for auth/proxy/gating, and matches
  the established monorepo pattern (one service per directory). Going live later
  = wrap the *same* executor around real `trading.place_order` behind the gate.

---

## 4. The canonical signal (the common denominator)

Every surface's signal reduces to the same tuple (confirmed across all ten
producers). The schema stores **`model_prob` and `reference_prob` separately** so
`edge` is derivable regardless of what "edge" means in that domain
(model−market for most, cross-source divergence for midterm, inter-venue spread
for arb):

| Field | Meaning | Example source |
|---|---|---|
| `source` | which producer (`crypto`, `stock`, `weather_bot`, `sports`, `midterm`, …) | dashboard key |
| `strategy` | sub-strategy within a source (`ensemble`, `rsi`, `sharp_divergence`, …) | free label |
| `venue` | where it settles (`polymarket`, `kalshi`, `alpaca`, `realized_price`, `election`) | resolver picks by this |
| `external_id` | market/entity id in that venue (`condition_id`, `ticker+window`, `race_key`) | resolver key |
| `market_question` | human description | audit/UI |
| `side` | normalized direction (`YES`/`NO`/`UP`/`DOWN`/`OVER`/`UNDER`) | |
| `model_prob` | the model's probability of the chosen side (0–1) | `Signal.model_prob` (`edge_calculator.py:24`) |
| `reference_prob` | the market/benchmark probability (0–1) | `Signal.market_prob` |
| `edge` | derived = `model_prob − reference_prob` (stored for convenience) | |
| `confidence` | model confidence (0–1 or high/med/low → normalized) | |
| `entry_price` | fill reference at emit time | |
| `resolution_time` | when the outcome is known (UTC) | `target_date`, `end_dt`, `commence_time` |
| `resolver_kind` | which resolver plugin settles it (§6) | enum |
| `resolver_args` | JSON blob the resolver needs (e.g. threshold parse) | |
| `dedup_key` | idempotency key (`source:external_id:window`) | prevents double-log |
| `emitted_at` | log time | |

This is a normalization of the existing shapes: weather `Signal`
(`polymarket_weather_bot/edge_calculator.py:20`), midterm forecast dict
(`midterm-dashboard/backend/forecast.py:174`), crypto `crypto_predictions`
(`crypto-dashboard/database.py:31`), sports edge dict
(`sports-dashboard/sports_dashboard.py:2909`), polymarket-bot trade dict
(`polymarket-bot/polymarket_bot.py:709`).

---

## 5. Data model (canonical ledger)

One DB owned by the engine, written **once in SQLite dialect** and translated to
Postgres on the fly — exactly the gateway idiom (`gateway/db.py`: `SCHEMA`
string at `:54`, `_to_pg_schema` at `:446`, `_translate` at `:295`, `conn()` at
`:424`, `_insert_returning_id` at `:346`, idempotent migrations in
`_init_db_migrations_{pg,sqlite}` at `:487/:503`). In production it shares the
gateway's Postgres instance with an `edge_` table prefix; in dev it's a local
`edge.db`.

```sql
-- Every signal a surface emits (append-only).
CREATE TABLE IF NOT EXISTS edge_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    strategy       TEXT NOT NULL DEFAULT '',
    venue          TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    market_question TEXT DEFAULT '',
    side           TEXT NOT NULL,
    model_prob     REAL NOT NULL,
    reference_prob REAL,
    edge           REAL,
    confidence     REAL,
    entry_price    REAL,
    resolution_time TEXT,           -- ISO8601 UTC
    resolver_kind  TEXT NOT NULL,
    resolver_args  TEXT DEFAULT '{}',
    dedup_key      TEXT NOT NULL,
    emitted_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(dedup_key)
);

-- Paper (later: live) position opened for a signal the executor chose to take.
CREATE TABLE IF NOT EXISTS edge_positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id      INTEGER NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'paper',  -- 'paper' | 'live'
    side           TEXT NOT NULL,
    qty            REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    stop_price     REAL,
    target_price   REAL,
    sizing_reason  TEXT DEFAULT '',                -- from the risk engine
    last_mark_price REAL,
    last_mark_at   TEXT,
    realized_pnl   REAL DEFAULT 0,
    fees_paid      REAL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'open',   -- open|closed|settled_win|settled_loss
    opened_at      TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at      TEXT
);

-- Ground-truth outcome for a signal (written by the resolver loop).
CREATE TABLE IF NOT EXISTS edge_resolutions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id      INTEGER NOT NULL,
    outcome        INTEGER,          -- 1 = chosen side won, 0 = lost
    settle_price   REAL,             -- final price / realized delta
    was_correct    INTEGER,          -- model direction matched
    resolver_kind  TEXT NOT NULL,
    resolved_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(signal_id)
);

-- Rolling per-source/strategy score (recomputed by the scorer).
CREATE TABLE IF NOT EXISTS edge_scores (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    strategy       TEXT NOT NULL DEFAULT '',
    window         TEXT NOT NULL DEFAULT 'all',   -- all|30d|90d
    n              INTEGER,
    win_rate       REAL,
    brier          REAL,
    brier_null     REAL,             -- base-rate / market Brier (the NULL)
    skill          REAL,             -- (null-model)/null
    ece            REAL,             -- expected calibration error
    expectancy     REAL,             -- avg R per signal
    net_ann_return REAL,             -- after costs
    sharpe         REAL,
    max_drawdown   REAL,
    edge_tstat     REAL,             -- Newey-West HAC t on edge-over-null
    edge_pvalue    REAL,
    edge_significant INTEGER,
    edge_below_cost INTEGER,
    computed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source, strategy, window)
);

-- Promotion state machine per source/strategy.
CREATE TABLE IF NOT EXISTS edge_source_status (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    strategy       TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT 'shadow',  -- shadow|paper|candidate|promoted|demoted
    gate_report    TEXT DEFAULT '{}',               -- JSON: which criteria pass/fail
    changed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    changed_by     TEXT DEFAULT 'gate',             -- 'gate' | operator email
    UNIQUE(source, strategy)
);

CREATE INDEX IF NOT EXISTS idx_edge_signals_source ON edge_signals(source, strategy);
CREATE INDEX IF NOT EXISTS idx_edge_signals_unresolved ON edge_signals(resolution_time);
CREATE INDEX IF NOT EXISTS idx_edge_positions_open ON edge_positions(status);
```

**Reuse, don't reinvent:** this is deliberately the union of the two existing
ledger idioms — the bot-style *signal+position+outcome+PnL* tables
(`polymarket_weather_bot/datastore.py:26`) and the dashboard *snapshot+resolution*
tables (`weather_resolutions`, `sports_edge_history`,
`crypto_predictions.was_correct`). `edge_positions` mirrors the gateway's
`user_positions` (`gateway/db.py:192`) so the eventual live path can share the
`MarkToMarketWorker` and `get_portfolio_summary` machinery verbatim.

---

## 6. Pluggable resolvers (reuse existing domain logic)

Each signal carries a `resolver_kind`. A resolver answers "did this signal's
chosen side win?" — or returns `None` if it can't settle yet. The engine ships a
registry; each entry **wraps logic that already exists**:

```python
class Resolution(TypedDict):
    outcome: int          # 1 win / 0 loss
    settle_price: float
    was_correct: bool

class Resolver(Protocol):
    kind: str
    def ready(self, sig: Signal, now: datetime) -> bool: ...   # resolution_time passed & data available
    def resolve(self, sig: Signal) -> Resolution | None: ...   # None = not yet
```

| `resolver_kind` | Wraps existing code | How it settles |
|---|---|---|
| `polymarket_market` | polymarket-bot `resolve_pending()` (`polymarket_bot.py:646`) + top-traders `resolved_markets.fetch_resolved_markets()` (`:93`) | Gamma `outcomePrices ≈ 1.0` after `end_dt` → **factor out one generic Polymarket resolver** |
| `kalshi_market` | `gateway/kalshi_client.get_positions/get_fills` settlement | contract settles YES/NO |
| `weather_temp` | `polymarket_weather_dashboard/backtest.py`: `parse_threshold` (`:76`), `fetch_observed_highs` (`:50`), `resolve_market` (`:114`) | Open-Meteo archive vs threshold |
| `price_direction` | `crypto-dashboard/database.resolve_prediction()` (`:297`) | realized delta over the window |
| `stock_next_day` | `stock-dashboard/stock_predictor_bot.resolve_pending_bets()` (`:745`) | next-day OHLC from yfinance |
| `sports_score` | `sports-dashboard/_auto_resolve_edges()` (`:3947`) | final scores fuzzy-matched to outcome |
| `election_result` | `midterm-dashboard/backend/historical_results.py` + `calibration.py` | actual winner's party |

A **resolver-loop worker** (same shape as `gateway/poller.py` /
`mark_to_market.MarkToMarketWorker` — asyncio loop, `start()/stop()/stats()`)
periodically pulls `edge_signals` past `resolution_time` with no
`edge_resolutions` row, calls the matching resolver, and on a non-`None` result
writes `edge_resolutions` **and** closes any open `edge_positions` with realized
PnL and `settled_win/settled_loss`. **This close-out writer is the one real gap
in the current codebase** — the gateway schema has `settled_win/settled_loss`
statuses but nothing writes them (`gateway/db.py:192`, confirmed no settlement
writer). The Edge Engine owns that step.

**Plugin precedent to copy:** the resolver registry mirrors
`midterm-dashboard/backend/aggregators/*` and
`crypto-trackers-dashboard/ingestion/*` — one module per source, discovered via a
registry dict. Adding a new domain = drop in one resolver module.

---

## 7. Paper executor (wraps the existing risk engine)

The executor turns an accepted signal into a paper position, going through the
**real risk engine** so the numbers mean the same thing they will in production:

```
signal ──▶ risk_engine.size(signal, bankroll, portfolio) ──▶ PositionSize
        ──▶ paper_fill(signal, size)  # {order_id:"paper_…", status:"filled", paper:True}
        ──▶ db.open_edge_position(...)
```

- **Sizing/stops/heat — default plug:** `stock-dashboard/risk/` is the richest
  and dataclass-clean: `PositionSizer.size_position` (vol/correlation/confidence
  Kelly + caps, `sizing.py:63`), `size_trade_from_risk` (R-based, `:164`),
  `StopManager` (ATR/trailing/breakeven, `stops.py`), `PortfolioHeatTracker`
  (exposure limits + `can_enter_trade`, `heat.py:272`). A lighter alternative is
  `polymarket_weather_bot/risk_manager.RiskManager` (Kelly + daily-loss halt +
  per-market cap, `:57`). The risk engine is **pluggable** behind a
  `RiskEngine` interface so a source can pick its sizing discipline; default =
  the stock risk package.
- **Paper fill contract — reuse verbatim:** the paper fill returns the exact
  shape the live path already uses —
  `polymarket_weather_bot/clob_client._paper_trade()` returns
  `{order_id:"paper_…", status:"filled", …, paper:True}` (`:113`), and
  `gateway/trading.place_order()` returns `{status, order_id, fill_price,
  shares}` (`gateway/trading.py:78`). The executor writes both to
  `edge_positions`. **Going live = swap `paper_fill` for
  `trading.place_order(...)`** behind the promotion flag; nothing else changes.
- **Mark-to-market:** a worker cloned from `gateway/mark_to_market.py` marks open
  `edge_positions` via `trading.get_mark_price()` (`gateway/trading.py:344`);
  unrealized PnL computed at read time (same as `get_portfolio_summary`).
- **Bankroll:** a single global paper bankroll per `(source, strategy)` so the
  track record reflects the *source's* skill, independent of any user. (Live
  positions later attach to real users via the existing `user_positions` path.)

---

## 8. Promotion gate (the honest core)

For each `(source, strategy)` the scorer recomputes `edge_scores` on a schedule
and the gate maps scores → state. This is where FMT's discipline is inherited
directly rather than reimplemented:

| Criterion | Threshold (tunable) | Reuse |
|---|---|---|
| Enough evidence | `n ≥ N_min` (e.g. 100 resolved) | count |
| Beats the null | `skill > 0` vs base-rate/market Brier | `core.skill` (`core.py:104`), `readout_baserate` (`pipeline.py:220`) |
| Well-calibrated | `ECE ≤ ε` (e.g. 0.05) | `calibration.expected_calibration_error` (`:138`), `reliability` (`:98`) |
| Positive expectancy | `expectancy > 0` (avg R) | R-multiple from `edge_positions` PnL |
| Edge survives costs | `edge_significant == 1` and not `edge_below_cost` | `harness._transaction_cost_report` → `edge_tstat`/`edge_pvalue`/`edge_significant`/`edge_below_cost` (`harness.py:134`), `_newey_west_tstat` (`:102`) |
| Risk-adjusted | `Sharpe > s_min`, `max_dd ≤ d_max` | `backtest.perf` (`:91`), `block_bootstrap_sharpe_diff` CI (`:110`) |

**State machine** (`edge_source_status.state`):

```
shadow ──(signals logged, no paper)──▶ paper ──(all gate criteria pass)──▶ candidate
   ▲                                                    │
   │                                                    ▼ operator approves
demoted ◀──(criteria regress)────────────────────── promoted (live-eligible)
```

- `shadow`: logged only (used while a resolver/executor is being validated).
- `paper`: paper-executed, accruing a track record.
- `candidate`: passed the gate — the engine *recommends* promotion.
- `promoted`: an operator flipped it live (human-in-the-loop; `changed_by` =
  operator email). Only promoted sources route to `trading.place_order`.
- `demoted`: criteria regressed (e.g. drawdown breach) → auto-demote, alert.

The gate **never auto-promotes to live**; it produces a `gate_report` JSON
(each criterion pass/fail + values) that the track-record dashboard renders as a
checklist. This keeps the "honest floor" philosophy: a source that doesn't beat
its null after costs is visibly *not promotable*, however good its raw win rate
looks.

---

## 9. Track-record dashboard (the surface)

**v1 shape — apex-rendered admin page** (fastest, matches `/admin/fleet`): a
gateway route `@app.get("/admin/edge")` guarded by `_require_admin_user`
(`gateway/server.py:2716`), rendering `static/edge.html` via `render_page`
(`:1093`), reading `edge_scores`/`edge_source_status` through new `db.py`
helpers. No new subdomain, no billing — internal-only while the track record is
being built.

**v2 shape — full docked subscriber product** (once there's a track record worth
selling): its own port/subdomain in `config.json`, SSO-gated, appears in `/app`
and the hub automatically (agent-confirmed: no `server.py` change needed to add
a config-driven dashboard). This is the "incremental go-live": flip
`hidden`/pricing in config.

**What it shows per source/strategy** (all reusing existing analytics):
- Signals / win rate / Brier / skill-vs-null and the **gate checklist**.
- **Reliability curve** (predicted vs realized) — `calibration.reliability`
  (`:98`) / `polymarket_weather_bot/datastore.get_brier_score()` (`:214`).
- **Paper equity curve + Sharpe/ROI/max-DD** —
  `sports-dashboard/_compute_pnl_simulation()` (`:3426`) already returns exactly
  `{n_bets, total_pnl, win_rate, roi_pct, sharpe, max_drawdown, equity_curve}`.
- Promotion state + "beats null after costs? ✅/❌" verdict.

---

## 10. Thin client (how surfaces emit)

Surfaces stay decoupled: they emit a canonical signal to the engine and never
block on it.

```python
# edge_client.py  — copied into (or pip-installed by) each dashboard
def emit_signal(**fields) -> None:
    """Fire-and-forget POST to the Edge Engine. Never raises into the caller."""
    try:
        httpx.post(f"{EDGE_ENGINE_URL}/api/signals",
                   json=_normalize(fields),
                   headers={"X-Edge-Secret": EDGE_INGEST_SECRET},
                   timeout=1.5)
    except Exception:
        log.debug("edge emit failed (non-fatal)")
```

- **Transport = HTTP POST**, not a shared DB import — keeps the engine's schema
  private and lets surfaces stay on their own SQLite. (Each producer already
  runs as its own service.)
- **Auth = a dedicated ingest secret** (`X-Edge-Secret`), separate from the
  gateway SSO secret, since emits are service-to-service, not user requests. The
  engine still sits behind the gateway for *human* traffic (the dashboard),
  using the standard SSO trust (`whale-dashboard/backend/auth.py` pattern:
  verify `X-Gateway-Secret` == `GATEWAY_SSO_SECRET`).
- **Idempotent:** the client sends `dedup_key`; the ingest endpoint upserts on
  it (`ON CONFLICT(dedup_key) DO NOTHING`), so retries and overlapping scans
  never double-count — the discipline the weather bot's unwired `log_calibration`
  never got.
- **Backfill:** a one-shot importer reads each surface's *existing* history
  (`crypto_predictions`, `sports_edge_history`, weather snapshots) into
  `edge_signals` so the track record doesn't start from zero.

---

## 11. Docking onto the gateway (concrete checklist)

Following the gateway's own "add a service" checklist (`gateway/README.md:126`,
verified end-to-end):

1. **Service:** `edge-engine/server.py` (FastAPI) on port e.g. `9000`, binding
   `0.0.0.0`; `GET /` health returns <500; SSO trust by copying
   `whale-dashboard/backend/auth.py`; `Dockerfile` copied from
   `gateway/Dockerfile`; `requirements.txt`.
2. **Config:** one `gateway/config.json` `dashboards` entry — key `"edge"`,
   `subdomain`, `target: 9000`, `display_name`, `accent`, `hidden: true`
   initially (admin/internal). Routing, gating, `/app`, billing all read from
   config by key — **no `server.py` change**.
3. **Gating:** admin-only for v1 via a gateway route guarded by
   `_require_admin_user`; subscriber-gating is automatic if/when it becomes a
   product (`cached_has_subscription`).
4. **DB:** `edge_*` tables added via the `SCHEMA`/`_init_db_migrations_*` idiom;
   in prod point `DATABASE_URL` at the shared Postgres; add a leg to
   `test_db_backends.py` (CI runs sqlite+postgres).
5. **Local dev:** add a start block + port to `start_dashboards.sh`.
6. **Docker:** an `edge:` service in `docker-compose.yml` (build, `expose 9000`,
   healthcheck) + gateway `depends_on`.
7. **Prod:** `deploy/narve-edge.service` (copy `narve-crypto.service`); add to
   `install-services.sh` `SERVICES` and gateway `Wants=`.
8. **CI/deploy:** add `edge-engine/` to the ruff list (`ci.yml`), an optional
   `test-edge` job, and to `deploy.yml` `SITES`/`SERVICES`.
9. **DNS:** `cloudflared tunnel route dns <tunnel> edge.narve.ai` (only if it
   becomes public).

**Concurrency caveat:** the gateway runs `workers=1` (`server.py:5051`); the Edge
Engine's scan/resolver/MTM loops are its *own* process, so that constraint
doesn't apply — but if the engine ever scales to >1 worker, put the loop
leadership + shared counters in Redis (already in the stack via `gateway/cache.py`).

---

## 12. End-to-end data flow

```
emit_signal() ─▶ POST /api/signals ─▶ upsert edge_signals (dedup)
              ─▶ paper executor: risk_engine.size → paper_fill → edge_positions(open)
   … time …
MTM worker    ─▶ mark open edge_positions (get_mark_price)
resolver loop ─▶ resolution_time passed? resolver.resolve() → edge_resolutions
              ─▶ close edge_positions: realized_pnl, settled_win/loss   ← the gap we fill
scorer        ─▶ recompute edge_scores (Brier, skill, ECE, expectancy, cost-sig, Sharpe)
gate          ─▶ update edge_source_status (shadow→paper→candidate); write gate_report
operator      ─▶ approves candidate → promoted   (human-in-the-loop)
promoted src  ─▶ executor routes to trading.place_order (LIVE) instead of paper_fill
dashboard     ─▶ renders scores, reliability curve, equity curve, gate checklist
```

---

## 13. Build phases (incremental, each shippable)

- **Phase 0 — Ledger + ingest + backfill.** `edge_signals`, `/api/signals`,
  `edge_client`, dedup, and a backfill importer for 2–3 already-wired surfaces
  (crypto, sports, weather). *Deliverable:* every new signal lands in one table.
- **Phase 1 — Resolvers + settlement writer.** Factor the generic Polymarket
  resolver; wrap the per-domain resolvers; resolver-loop worker;
  `edge_resolutions` + the close-out writer (fills the codebase gap).
  *Deliverable:* honest resolved outcomes for every logged signal.
- **Phase 2 — Paper executor + risk wrap + MTM.** `RiskEngine` interface
  (default = stock risk package), paper fills, `edge_positions`, MTM worker.
  *Deliverable:* a real paper P&L per source.
- **Phase 3 — Scorer + promotion gate + dashboard.** `edge_scores`,
  `edge_source_status`, FMT-based stats, the admin track-record page.
  *Deliverable:* "which signals are promotable" answered honestly.
- **Phase 4 — Live docking.** Behind the gate: route `promoted` sources to
  `trading.place_order`; attach live fills to `user_positions`; optional public
  product surface. *Deliverable:* incremental, gated go-live.

---

## 14. Reuse map (what to reuse / refactor / build)

| Component | Reuse directly | Refactor to reuse | Build new |
|---|---|---|---|
| DB dual-backend | `gateway/db.py` idioms (`SCHEMA`, `_translate`, `conn`, `_to_pg_schema`) | — | `edge_*` tables + helpers |
| Signal shape | weather `Signal`, midterm forecast dict, crypto/sports/pmbot dicts | normalize into one schema | `edge_client.emit_signal` |
| Resolvers | weather `resolve_market`, crypto `resolve_prediction`, stock `resolve_pending_bets`, sports `_auto_resolve_edges`, election `historical_results` | one generic Polymarket resolver (from pmbot + top-traders) | resolver registry + loop |
| Risk/sizing | `stock-dashboard/risk/{sizing,stops,heat}`, `polymarket_weather_bot/risk_manager` | `RiskEngine` interface | — |
| Execution | `_paper_trade` shape, `trading.place_order` contract | — | executor + **settlement/close-out writer** |
| Mark-to-market | `gateway/mark_to_market.py`, `trading.get_mark_price` | clone worker for `edge_positions` | — |
| Scoring | FMT `core`/`harness`/`calibration`/`eventmetrics`, sports `_compute_pnl_simulation`, weather `get_brier_score` | expose `predict_live` as return-value (currently prints) | scorer + gate |
| Dashboard | `render_page`, `/admin/fleet` pattern, sports track-record endpoints | — | `static/edge.html` + routes |
| Docking | gateway config/proxy/SSO, `whale-dashboard/backend/auth.py` | — | config entry + deploy/CI wiring |

---

## 15. Risks & open decisions

**Risks / mitigations**
- **Look-ahead leakage** (scoring a signal with data it couldn't have had): the
  ledger stamps `emitted_at` and resolvers only read data at/after
  `resolution_time`; scoring is walk-forward — inherit FMT's causal windowing
  discipline (`harness.slice_market_data:86`).
- **Unresolvable surfaces** (world-state indices, crypto-trackers conditions):
  excluded from v1; admissible only once an outcome is bound (e.g. tie a
  "severity EXTREME" alert to a Polymarket conflict market).
- **Small-sample promotion:** the `N_min` + significance-after-costs gate stops a
  hot streak from being promoted; block-bootstrap CI guards Sharpe.
- **Emit path reliability:** fire-and-forget with a 1.5s timeout so a slow engine
  never degrades a dashboard's request path.

**Open decisions for the operator (recommendations in bold)**
1. Default risk engine to wrap — **stock-dashboard/risk package** (richest:
   R-sizing, stops, heat) vs weather bot's simpler RiskManager.
2. Track-record surface starts **admin-only apex page** (v1) → subscriber product
   later.
3. Ingest transport — **HTTP POST + dedicated ingest secret** vs shared-DB import.
4. Track record is **global per (source, strategy)** for skill measurement; live
   positions attach per-user via the existing `user_positions` path.
5. Promotion is **human-in-the-loop** in v1 (gate recommends, operator approves).

---

## 16. Appendix — key anchor points in the existing code

- Signal shapes: `polymarket_weather_bot/edge_calculator.py:20`,
  `midterm-dashboard/backend/forecast.py:174`, `crypto-dashboard/database.py:31`,
  `sports-dashboard/sports_dashboard.py:2909`, `polymarket-bot/polymarket_bot.py:709`.
- Resolvers: `polymarket_weather_dashboard/backtest.py:114`,
  `crypto-dashboard/database.py:297`,
  `stock-dashboard/stock_predictor_bot.py:745`,
  `sports-dashboard/sports_dashboard.py:3947`,
  `polymarket-bot/polymarket_bot.py:646`,
  `top-traders-dashboard/resolved_markets.py:93`.
- Risk/execution: `stock-dashboard/risk/{sizing,stops,heat}.py`,
  `polymarket_weather_bot/risk_manager.py:57`,
  `polymarket_weather_bot/clob_client.py:113`, `gateway/trading.py:78`,
  `gateway/mark_to_market.py:31`.
- Stats/scoring: `financial-matrix-toolkit/{core,harness,backtest,calibration,eventmetrics}.py`,
  `sports-dashboard/sports_dashboard.py:3426`,
  `polymarket_weather_bot/datastore.py:214`.
- DB idiom: `gateway/db.py:{54,192,295,346,424,446,487,503,772}`.
- Docking: `gateway/server.py:{896,911,1093,2716,4657}`, `gateway/config.json`,
  `whale-dashboard/backend/auth.py`, `gateway/README.md:126`.

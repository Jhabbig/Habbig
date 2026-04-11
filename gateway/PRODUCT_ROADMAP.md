# narve.ai — 15-Feature Product Build Plan

## Context

narve.ai is an invite-only prediction market intelligence platform. The core product scrapes Twitter/TruthSocial for predictions, scores source credibility using a Bayesian engine, and surfaces high-EV bets. The codebase has a working auth system, multi-dashboard architecture, Claude AI integration, Polymarket/Kalshi trading, and an email notification pipeline. However, the credibility engine's recomputation is a placeholder, market resolution is manual-only, and the betyc signal fields on markets are never populated. These 15 features fill those gaps and build the product into a genuinely irreplaceable trading intelligence platform.

**User instruction: Save this plan. Do not push any code.**

---

## Dependency Graph

```
F1 (Credibility Recompute) ──┬──> F4 (Edge Score) ──┬──> F5 (False Consensus)
                              │                      ├──> F7 (Morning Briefing)
                              │                      ├──> F8 (Market Mover Alerts)
                              │                      ├──> F12 (Developer API)
                              │                      ├──> F13 (Backtesting)
                              │                      ├──> F14 (Whale Intelligence)
                              │                      ├──> F15 (Telegram Bot)
                              │                      └──> F16 (Kelly Criterion)
                              │
F2 (Resolution Detection) ────┤
                              ├──> F6 (Retrospective)
                              └──> F9 (Calibration)

F10 (Claude Extraction) ──────> F11 (Metaculus + Substack)
```

## Migration Sequence

| #   | Slug                    | Feature |
|-----|-------------------------|---------|
| 010 | credibility_pipeline    | F1      |
| 011 | retrospectives          | F6      |
| 012 | calibration             | F9      |
| 013 | morning_briefing        | F7      |
| 014 | api_keys                | F12     |
| 015 | backtests               | F13     |
| 016 | whale_positions         | F14     |
| 017 | user_bankroll           | F16     |
| 018 | telegram_links          | F15     |

F2, F4, F5, F8, F10, F11 need no migrations (use existing tables or in-memory only).

## New Files Summary

| Path | Feature | Purpose |
|------|---------|---------|
| `migrations/010_credibility_pipeline.py` | F1 | Index for resolved predictions |
| `migrations/011_retrospectives.py` | F6 | resolution_retrospectives table |
| `migrations/012_calibration.py` | F9 | source_calibration table |
| `migrations/013_morning_briefing.py` | F7 | User preference columns |
| `migrations/014_api_keys.py` | F12 | api_keys table |
| `migrations/015_backtests.py` | F13 | backtests table |
| `migrations/016_whale_positions.py` | F14 | whale_positions table |
| `migrations/017_user_bankroll.py` | F16 | User bankroll/kelly columns |
| `migrations/018_telegram_links.py` | F15 | telegram_user_links table |
| `jobs/resolution_jobs.py` | F2 | Market resolution polling |
| `intelligence/retrospective.py` | F6 | Claude retrospective analysis |
| `intelligence/prediction_extractor.py` | F10 | Claude prediction extraction |
| `intelligence/backtester.py` | F13 | Backtesting engine |
| `backend/markets/whale_tracker.py` | F14 | Whale position tracking |
| `scraper/scrapers/metaculus.py` | F11 | Metaculus API scraper |
| `scraper/scrapers/substack.py` | F11 | Substack RSS scraper |
| `api_v1.py` | F12 | Versioned developer API router |
| `integrations/__init__.py` | F15 | Integrations package |
| `integrations/telegram_bot.py` | F15 | Telegram bot |
| `email_system/templates/morning_briefing.html` | F7 | Morning email template |
| `email_system/templates/market_mover_alert.html` | F8 | Mover alert template |
| `email_system/templates/resolution_retrospective.html` | F6 | Retrospective email |

---

## SPRINT 1 (Week 1): Foundation

### F1: Credibility Auto-Recomputation Pipeline

**Problem:** `recompute_all_credibilities()` in db.py is a placeholder that returns count but never actually recomputes. `decay_weighted_accuracy` field exists but is never computed.

**Algorithm:**
- For each source with resolved predictions:
  - Exponential time-decay: `weight = exp(-0.01 * age_days)` (half-life ~69 days)
  - Decay-weighted accuracy: `sum(correct_i * weight_i) / sum(weight_i)`
  - Bayesian smoothing: `(n * dwa + 10 * 0.5) / (n + 10)` (prior=0.5, strength=10)
  - `accuracy_unlocked = True` when `total_predictions >= 10`
  - Per-category breakdown using same algorithm
- Run every 6 hours via 4 cron entries

**Files to modify:**
- `db.py`: Replace placeholder body of `recompute_all_credibilities()` (~line 1948) with full algorithm
- `jobs/pipeline_jobs.py`: Add `@register_job("recompute_credibilities")` + 4 cron entries (00:15, 06:15, 12:15, 18:15 UTC)
- `jobs/__init__.py`: Ensure pipeline_jobs is imported (already is)

**Migration 010:** Add index `idx_predictions_resolved ON predictions(resolved, source_handle)` to speed the recomputation query.

**Key function signature:**
```python
def recompute_all_credibilities() -> int:
    """Recompute all source credibility scores using Bayesian time-decay.
    Returns number of sources recomputed."""
```

**Tests:** `tests/test_credibility_recompute.py`
- 0 resolved predictions -> returns 0
- 5 resolved -> accuracy_unlocked = False
- 15 resolved -> accuracy_unlocked = True
- Recent correct predictions produce higher score than old ones
- Low N regresses toward 0.5 prior
- Per-category breakdown works

---

### F2: Market Resolution Auto-Detection

**Problem:** Markets are resolved only via manual admin action (POST `/admin/markets/{slug}/mark-resolved`). No automatic polling.

**Approach:**
- Hourly cron job polls Polymarket Gamma API and Kalshi API for resolved markets
- Matches resolved markets to predictions via `market_id` field (format: `poly:{slug}` or `kalshi:{ticker}`)
- Marks matching predictions as `resolved=1, resolved_correct=0/1`
- Triggers credibility recomputation + notification jobs

**New file:** `jobs/resolution_jobs.py`

**New db.py functions:**
```python
def get_unresolved_market_ids() -> list[str]:
    """Return distinct market_ids that have unresolved predictions."""

def resolve_predictions_for_market(market_id: str, outcome_yes: bool) -> int:
    """Mark all unresolved predictions for market as resolved. Returns count."""
```

**Job:**
```python
@register_job("poll_market_resolutions")
async def poll_market_resolutions() -> dict:
    # For each unresolved market_id:
    #   If poly: -> await poly.get_market(slug), check resolved/outcome fields
    #   If kalshi: -> await kalshi.get_market(ticker), check status == "settled"
    # After batch: enqueue "recompute_credibilities" if any resolved

register_cron("poll_market_resolutions", minute=17)  # hourly at :17
```

**Critical detail:** Polymarket market response has `resolved: bool` and `outcome: str`. Kalshi has `status: "settled"` and `result: "yes"/"no"`. Both clients already support `get_market()`.

**Tests:** `tests/test_resolution_polling.py`
- Mock API responses, test prediction marking
- Test credibility recompute is triggered
- Test idempotency (already-resolved skipped)

---

## SPRINT 2 (Week 2): Edge Scoring

### F4: Information Asymmetry Score ("Top Edge Markets")

**Problem:** `betyc_ev_score`, `betyc_avg_credibility`, `betyc_prediction_count`, `betyc_consensus` on UnifiedMarket are always None/0. `calculate_betyc_probability()` exists and works but is never called during market listing.

**Approach:**
- New function `enrich_markets_with_intelligence(markets)` in `unified_markets.py`
- For each active market: call `db.get_predictions_for_market(market.id)`, then `db.calculate_betyc_probability(pred_dicts)`, then populate the betyc fields
- New endpoint: `GET /api/markets/top-edge` returns markets sorted by `|betyc_edge|` descending
- **CRITICAL:** This endpoint MUST be registered BEFORE the `/{market_id:path}` catch-all in server.py

**Files to modify:**
- `backend/markets/unified_markets.py`: Add `enrich_markets_with_intelligence()` function
- `server.py`: Add `GET /api/markets/top-edge` endpoint before the path-converter routes (~line 5070)

**Key function:**
```python
async def enrich_markets_with_intelligence(markets: list[UnifiedMarket]) -> list[UnifiedMarket]:
    """Populate betyc_* fields using prediction data and credibility scores."""
```

**Tests:** `tests/test_edge_scoring.py`

---

### F5: False Consensus Detection

**Problem:** No way to detect markets where crowd consensus (high price) disagrees with credible source intelligence.

**Approach:**
- Add `false_consensus: bool` and `false_consensus_direction: Optional[str]` to UnifiedMarket dataclass
- In `enrich_markets_with_intelligence()`: flag when `market.yes_price > 0.80` (or < 0.20) AND `|betyc_edge| > 0.15`
- New endpoint: `GET /api/markets/false-consensus`
- Frontend: yellow warning badge in trade.js market table

**Files to modify:**
- `backend/markets/unified_markets.py`: Add fields + detection logic
- `server.py`: Add endpoint
- `static/trade.js`: Add badge rendering (~line 536-598 market table)

**Tests:** `tests/test_false_consensus.py`

---

## SPRINT 3 (Week 3): Retrospective + Calibration

### F6: Post-Resolution Retrospective

**Problem:** When markets resolve, there's no analysis of how narve.ai's intelligence performed.

**Approach:** Follow the `intelligence/environmental.py` pattern exactly:
- New module `intelligence/retrospective.py` with Claude analysis
- System prompt instructs Claude to analyze: market question, outcome, predictions with credibility, who called it early, who was wrong
- Cache in new `resolution_retrospectives` table
- Trigger from resolution_jobs.py after market resolves
- Send as email to users who viewed/saved the market
- Use Haiku model for cost

**New files:**
- `intelligence/retrospective.py`
- `email_system/templates/resolution_retrospective.html`
- `migrations/011_retrospectives.py`

**Migration 011:** `resolution_retrospectives` table (market_id, market_question, outcome, betyc_consensus_was, market_price_was, analysis_text, top_correct_sources JSON, top_wrong_sources JSON, generated_at, generated_by)

**Config:** `RETROSPECTIVE_MODEL` env var (default `claude-haiku-4-5-20250929`)

**Tests:** `tests/test_retrospective.py`

---

### F9: Calibration Scoring

**Problem:** Credibility only measures accuracy (right/wrong). Doesn't measure calibration (when source says 80%, does the event happen 80% of the time?).

**Approach:**
- Bucket each source's predictions by stated probability (0-10%, 10-20%, ..., 90-100%)
- Compute actual resolution rate per bucket
- Calibration score = `1 - mean(|actual_rate - predicted_avg|)` per bucket
- Store in new `source_calibration` table
- Add to credibility recompute job (compute calibration alongside accuracy)
- Display on source profile page as calibration curve

**New db.py function:**
```python
def compute_calibration(source_handle: str) -> Optional[dict]:
    """Returns {calibration_score, buckets: [{range, predicted, actual, count}], total_calibrated}"""
```

**Migration 012:** `source_calibration` table (source_handle UNIQUE, calibration_score, calibration_data JSON, total_calibrated, last_computed_at)

**New endpoint:** `GET /api/credibility/{source_handle}/calibration`

**Tests:** `tests/test_calibration.py`

---

## SPRINT 4 (Week 4): Engagement

### F7: Morning Intelligence Briefing

**Problem:** No habit loop. Traders must actively visit the site.

**Approach:**
- Daily personalized email at 08:00 UTC (configurable per user)
- Content: top 5 markets by |betyc_edge|, new predictions from followed sources (last 24h), markets approaching resolution
- New email template following `weekly_digest.html` pattern
- New cron job `send_morning_briefings` at 08:03 UTC
- Gated to Pro tier users with `morning_briefing_enabled=1`

**Migration 013:** Add `morning_briefing_enabled INTEGER DEFAULT 0` and `morning_briefing_hour INTEGER DEFAULT 8` columns to users.

**New file:** `email_system/templates/morning_briefing.html`

**Job in `jobs/email_jobs.py`:**
```python
@register_job("send_morning_briefings")
async def send_morning_briefings() -> dict:
    # Query users with morning_briefing_enabled=1
    # For each: build context (top edge markets, followed source activity, approaching resolutions)
    # Enqueue email per user

register_cron("send_morning_briefings", hour=8, minute=3)
```

**Tests:** `tests/test_morning_briefing.py`

---

### F8: Market Mover Alerts

**Problem:** No real-time notification when a market moves significantly and narve.ai had intelligence on it.

**Approach:**
- Cron job every 15 minutes compares current prices to 2h-ago snapshot
- When `|price_change| > 0.08` AND market has credibility intelligence:
  - Find matching high-credibility predictions
  - Send notification with source attribution
- Respect user's `notify_ev_threshold` and `notify_cred_threshold` columns (already exist)

**New file:** `email_system/templates/market_mover_alert.html`

**Job in `jobs/notification_jobs.py`:**
```python
@register_job("check_market_movers")
async def check_market_movers(price_change_threshold: float = 0.08, lookback_hours: int = 2) -> dict:
    # Compare current market prices to snapshots from lookback_hours ago
    # Filter by threshold + credibility intelligence
    # Dispatch notifications to opted-in users

register_cron("check_market_movers", minute=32)  # hourly at :32
```

**Uses existing:** `market_snapshots` table, `get_market_snapshot_at()`, `notify_email`/`notify_ev_threshold` user columns.

**Tests:** `tests/test_market_movers.py`

---

## SPRINT 5 (Week 5): AI Extraction + New Sources

### F10: Claude-Powered Prediction Extraction

**Problem:** Scraper extracts raw posts but prediction identification relies on keyword matching. Misses sarcasm, conditional predictions, non-standard phrasing.

**Approach:**
- New module `intelligence/prediction_extractor.py` (follow environmental.py pattern)
- Claude Haiku processes batches of 10-20 posts
- Extracts: is_prediction (bool), direction (YES/NO), probability, category, market_keywords
- Structured JSON output
- Rate limited: 100 calls/hour max
- New pipeline job step between scraping and storage

**Key function:**
```python
async def extract_predictions_from_posts(posts: list[dict]) -> list[dict]:
    """Process a batch through Claude Haiku. Returns extraction results."""
```

**Config:** `EXTRACTION_MODEL` (default `claude-haiku-4-5-20250929`), `EXTRACTION_ENABLED` (default true)

**Cost estimate:** ~$0.01-0.05 per post at Haiku pricing. With 100 posts/hour from scrapers, ~$1-5/day.

**Tests:** `tests/test_prediction_extractor.py`

---

### F11: Metaculus + Substack Scraping

**Problem:** Only two data sources (Twitter, TruthSocial). Need more for better credibility data.

**Decision:** Metaculus (public API, forecaster track records) + Substack newsletters (high-quality analysis via RSS). NOT Reddit (user says not trustworthy).

**Metaculus scraper:** (`scraper/scrapers/metaculus.py`)
- Extends `BaseScraper` (from `scraper/scrapers/base.py`)
- Uses Metaculus API v2: `https://www.metaculus.com/api2/`
- Fetches questions with community predictions, forecaster data
- Source handles: `metaculus:{question_id}` or `metaculus:{username}`
- No auth needed (public API)

**Substack scraper:** (`scraper/scrapers/substack.py`)
- Extends `BaseScraper`
- RSS feed parsing via `feedparser` library
- Configurable feed list (Silver Bulletin, prediction-focused newsletters)
- Uses F10 (Claude extraction) for long-form prediction identification
- Source handles: `substack:{publication_slug}`

**New dependency:** `feedparser` in `scraper/requirements.txt`

**Tests:** `tests/test_metaculus_scraper.py`, `tests/test_substack_scraper.py`

---

## SPRINT 6 (Week 6): Enterprise

### F12: Credibility-Scored API for Developers/Quants

**Problem:** No programmatic access for quant funds or bot builders. All endpoints are session-cookie-authenticated.

**Approach:**
- New `api_v1.py` module with FastAPI APIRouter (prefix `/api/v1`)
- Bearer token auth via API keys (SHA-256 hashed, stored in `api_keys` table)
- Rate limited: 1000 req/hr (standard), 10000 req/hr (enterprise)
- Admin panel: API key generation and management

**Migration 014:** `api_keys` table (key_hash, key_prefix, user_id, name, tier, rate_limit_hour, created_at, last_used_at, revoked_at)

**Endpoints:**
```
GET  /api/v1/sources                          — all sources with credibility
GET  /api/v1/sources/{handle}                 — full source profile + calibration
GET  /api/v1/predictions?category=&source=    — paginated predictions
GET  /api/v1/markets/edge?min_sources=        — top edge markets
GET  /api/v1/markets/{id}/intelligence        — full market intelligence
```

**Files:**
- New: `api_v1.py`
- Modify: `server.py` to mount router: `app.include_router(api_v1.router)`
- New db.py functions: `get_api_key_by_hash()`, `create_api_key()`, `revoke_api_key()`, `touch_api_key()`, `list_user_api_keys()`

**Tests:** `tests/test_api_v1.py`

---

### F13: Backtesting Engine

**Problem:** No way to prove the credibility engine produces alpha. Sceptical traders and institutions need backtested proof.

**Approach:**
- Given: historical predictions + market snapshots + resolution outcomes
- Simulate: "bet on every market where betyc_edge > threshold"
- Parameters: min_credibility, min_edge, category_filter, bet_sizing (flat/kelly/half-kelly), bankroll
- Output: total_return, sharpe_ratio, win_rate, max_drawdown, trade_log
- Runs as async job (may take seconds)

**Migration 015:** `backtests` table (user_id, params JSON, status, result JSON, created_at, completed_at)

**New file:** `intelligence/backtester.py`

**Key function:**
```python
def run_backtest(params: dict) -> dict:
    """Simulate trading on historical data. Returns performance metrics."""
```

**Endpoints:**
```
POST /api/backtests              — submit params, returns backtest_id
GET  /api/backtests/{id}         — get results (poll until status=completed)
```

**Tests:** `tests/test_backtester.py`

---

## SPRINT 7 (Week 7): Whale Intelligence + Kelly

### F14: On-Chain Whale Intelligence Layer

**Problem:** No integration of on-chain capital flow data with credibility intelligence.

**Approach:**
- Track large Polymarket positions via Gamma API positions endpoint
- Maintain list of known whale wallets (seeded from leaderboard)
- Classify into tiers: $5K, $25K, $100K, $500K+
- Correlate with betyc intelligence: convergence (whale + credible sources agree) vs divergence (they disagree)
- Display convergence/divergence badges on market cards

**Migration 016:** `whale_positions` table (wallet_hash, market_id, side, amount_usd, tier, detected_at)

**New file:** `backend/markets/whale_tracker.py`

**Cron job:** `poll_whale_positions` at minute=47 each hour

**Endpoint:** `GET /api/markets/{market_id}/whales`

**Config:** `WHALE_WALLETS` env var (comma-separated addresses) or `WHALE_MIN_POSITION` (default 5000)

**Tests:** `tests/test_whale_tracker.py`

---

### F16: Kelly Criterion Bet Sizing

**Problem:** Traders know which markets have edge but not how much to bet.

**Approach:**
- Kelly formula: `f* = (p * b - q) / b` where `p = betyc_probability`, `q = 1-p`, `b = (1/market_price - 1)`
- Half-Kelly default (more conservative)
- Needs user's stated bankroll (new user preference)
- Display on market detail: "Recommended position: $X (half-Kelly)"

**Migration 017:** Add `bankroll REAL` and `kelly_fraction REAL DEFAULT 0.5` columns to users.

**New function:**
```python
def compute_kelly_sizing(betyc_probability, market_yes_price, bankroll, fraction=0.5) -> dict:
    """Returns {kelly_full_fraction, kelly_adjusted_fraction, recommended_amount, edge}"""
```

**Modify:** Market detail API response to include kelly data when user has bankroll set.
**Modify:** `static/trade.js` to display recommended position size in bet form.

**Tests:** `tests/test_kelly.py`
- Zero edge -> kelly = 0
- Negative edge -> kelly = 0 (not negative)
- Half vs full Kelly
- Various bankroll sizes

---

## SPRINT 8 (Week 8): Telegram Bot

### F15: Telegram Bot

**Problem:** Email is too slow for trading alerts. Traders live in Telegram.

**Approach:**
- Start with Telegram only (Discord/Slack later if needed)
- Bot commands: `/subscribe <link_code>`, `/edge [limit]`, `/source @handle`, `/alerts on|off`
- Alert dispatch: extend notification_jobs.py with Telegram delivery channel
- Start bot polling loop alongside FastAPI server (if `TELEGRAM_BOT_TOKEN` is set)

**Migration 018:** `telegram_user_links` table (user_id, telegram_chat_id UNIQUE, telegram_username, linked_at, alerts_enabled)

**New dependency:** `python-telegram-bot>=21.0` in requirements.txt

**New files:** `integrations/__init__.py`, `integrations/telegram_bot.py`

**Config:** `TELEGRAM_BOT_TOKEN` env var

**Tests:** `tests/test_telegram_bot.py`

---

## Key Risks and Mitigations

1. **SQLite write contention:** Multiple cron jobs writing simultaneously. Mitigated by WAL mode + short transactions. Credibility recompute should batch writes.

2. **Claude API costs:** F6 (retrospective) + F10 (extraction) both call Claude. Use Haiku for both. F10 needs strict rate limiting (100 calls/hour max).

3. **Polymarket API rate limits:** F2 + F14 poll hourly each. Combined with existing fetching, ~3 API calls/minute. Add exponential backoff on 429s.

4. **Route ordering in server.py:** New endpoints like `/api/markets/top-edge` MUST be registered BEFORE `/{market_id:path}` catch-all routes.

5. **Job module imports:** New job modules MUST be imported in `jobs/__init__.py` for decorators to register.

---

## Verification Plan

After each sprint:
1. `python3 -m pytest tests/ --tb=short -q` — all tests pass
2. `python3 -m ruff check server.py db.py server_features.py --select F,E9` — no errors
3. `python3 -c "import server; print('OK')"` — server imports cleanly
4. Manual smoke test of new endpoints via TestClient
5. Check new cron jobs appear in job queue status

For the full build:
- Run `migrations.upgrade_to_head()` on a fresh DB
- Verify all 18 migrations (001-018) apply cleanly
- Run full test suite: target 600+ passing, 0 failing
- Manual end-to-end: scrape -> extract -> store -> resolve -> recompute -> display edge

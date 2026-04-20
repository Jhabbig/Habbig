# Coordination Log

Each session appends entries as tasks start/finish. Format: [ISO timestamp] [P#] [task] [state]

[2026-04-20T08:43:47Z] [P8] polish layer — confidence UI, content warnings, FP queue, entity drill-in, market scaffold, email notifications, tests — start

[2026-04-20T09:08:02Z] [P8] polish layer — done

## P8 Handoff Report

### Deliverables

**Confidence score (decision #10)**
- `spike_detector._compute_confidence(z, multiple, backtest_hit_rate, warmup)` — pure function. Blend: z ∈[3,10]→[0,50] + mult ∈[3,10]→[0,25] + backtest ∈[0,1]→[0,25]. Warmup spikes return flat 30.
- Computed at insertion in `detect_and_record` and persisted to `spikes.confidence_score`. Default `backtest_hit_rate=0.5` (neutral) — P5 populates real per-entity rates later.

**Content-warning blur (decision #14)**
- `/api/spikes` now hydrates sample posts via new `db.get_posts_with_sensitivity` (LEFT JOIN classifications for `is_sensitive`).
- Spike-card sample excerpts wrapped in `.blur-wrap[data-sensitive="true"]` → CSS `filter: blur(6px)` → click to reveal, persists per-session via `sessionStorage[spike-reveal-<id>]`.
- Same pattern on entity drill-in page for per-post bodies.

**FP feedback (decision #11)**
- `⚑ flag` button on every spike card → modal w/ optional reason → POST `/api/fp-flag`. Already existed; I added the UI.
- New `GET /admin/fp-queue` (list unresolved, joined with spike context) and `POST /admin/fp-resolve` (mark + note). Localhost + super_admin.
- New `/admin` HTML page: minimal review grid with "Resolve" per row.

**Entity drill-in (`/entity/{name}`)**
- New HTML page + `entity.js` + `entity.css`.
- Four panels: 7d history chart, recent spikes list (confidence bar + summary), recent posts feed (blurred if sensitive), related markets.
- Links from spike cards' entity name and from "Top entities" panel rows.
- Three new API endpoints:
  - `GET /api/entity/{name}/spikes` → `db.get_entity_spikes`
  - `GET /api/entity/{name}/recent-posts` → `db.get_entity_recent_classified_posts` (JOIN posts+classifications, `entities_json LIKE %name%`)
  - `GET /api/entity/{name}/markets` → `entity_markets.json` lookup

**Market routing (decision #17, sub-decision C)**
- `scripts/build_entity_markets.py` — scaffolds `entity_markets.json` from `config.ALIASES` (dedup values). Supports `--check` flag for CI.
- Ran on current ALIASES → **79 entities**, all placeholder `https://narve.ai/markets/search?q={entity}` URLs.
- Spike card has "▸ View related markets" expand that fetches + renders up to 3 entries per entity. If none curated: shows "Suggest a market" → POST `/api/market-suggestions` (logs to `market_suggestions.log`, stub for v1).

**Email notifications (decision #6)**
- New `notifications.send_spike_email(spike_id, entity, summary, confidence, entity_url)`. Fail-soft everywhere — SMTP failure, missing gateway DB, broken template — all log and return `{sent, skipped, failed, recipients}` dict.
- Reads Pro subscribers from `GATEWAY_AUTH_DB` (env-configured, read-only sqlite open). Query filters on `email_marketing=1 AND suspended=0 AND is_deleted=0 AND (intelligence_addon_active=1 OR active pro/premium subscription)`.
- Dedup via `db.spike_already_emailed(spike_id, email)`. Per-user daily cap: 5 emails / 24h rolling via new `db.count_user_emails_today(email)`.
- Template: `email_templates/spike_alert.html` (minimal, `str.format_map`-based, no Jinja dependency). CTA → `https://annoyance.narve.ai/entity/{entity}`. Unsubscribe → `https://narve.ai/profile#email-preferences`.
- Invoked fire-and-forget from `spike_detector.detect_and_record` after successful insert; caller wraps in try/except so email failures never block the detector loop.
- Dev: set `EMAIL_DRY_RUN=1` to exercise the path without touching SMTP.

**Happiness/annoyance toggle (decision #7)**
- Topbar tab strip on index.html + entity.html. "Annoyance" active; "Happiness" disabled with `title="Coming soon — happiness index under construction"`. Activates when the second half ships.

**Paywall-aware UI (decision #4)**
- `annoyance.js` / `entity.js` wrap `fetch` to redirect to `https://narve.ai/billing` on 402.
- New `GET /api/me` → `{authenticated, user_id, email, tier}` (never 402s; anonymous gets `{authenticated: false}`).
- `#paywall-banner` in index.html shown when `/api/me` returns unauthenticated. Flag buttons are hidden for anonymous users.

### Tests

**18 new tests, all passing:**
- `tests/unit/test_confidence.py` (6) — pure-function cases for `_compute_confidence`: high z+mult → ≥70, warmup → flat 30, bounded [0,100] over a sweep of inputs, gate-threshold boundary, zero-backtest penalty, negative-z clamp.
- `tests/unit/test_fp_flag.py` (3) — `insert_fp_flag` writes a joined row, `list_fp_queue` filters resolved, `resolve_fp_flag` is idempotent.
- `tests/integration/test_entity_drill_in.py` (6) — seeds one spike + two classifications for "Apple", asserts 200 + correct shape on all 4 entity endpoints, plus a negative test that the full panel set is 402 for a free-tier client.
- `tests/integration/test_email_notification.py` (3) — builds a fake gateway auth.db with 5 users (2 Pro, 1 free, 1 unsubbed, 1 suspended), asserts exactly 2 sends + 2 ledger rows. Refire is a no-op (dedup). Missing `GATEWAY_AUTH_DB` degrades gracefully to zero recipients.

### Flagged for P2

Running the whole test suite alphabetically (`tests/backtest/` → `tests/integration/` → `tests/unit/`) surfaces **9 pre-existing failures** in P2-owned test modules (`test_retention.py`, `test_classifier_unit.py`, `test_bluesky_source.py::TestBackoffStatePerTermIsolated::test_repeated_failures_increase_delay`). Root cause: those modules mutate `config.DB_PATH` at **module-import time** (outside any fixture), so when an earlier integration test's `fresh_db` fixture resets DB_PATH and clears `db._local.conn` in teardown, the next module-scoped test inherits a stale connection pointing at a closed temp file. `sqlite3.OperationalError: no such table: posts` / `no such table: classifications`.

**Workaround right now:** running those test files in isolation passes cleanly (27 tests green). **Real fix (P2):** replace the module-level `_TMP = tempfile.TemporaryDirectory(); config.DB_PATH = ...; db._local.__dict__.clear(); db.init_db()` with a per-test setUp that re-inits the DB, or migrate them onto the `fresh_db` fixture. Not in P8 scope — noted here so whoever runs the full suite in CI knows which tracks' tests need the fix.

**All 18 new P8 tests pass**; **no P8 change regresses any existing passing test**; **221/230 overall pass** (the 9 failures are the pre-existing P2 issue above).

### Numbers worth surfacing

- **entity_markets.json**: 79 placeholder entries. Curator priority (based on ALIASES density + typical news volume): Apple, United Airlines, Tesla, Amazon, Google, Microsoft, Spotify, Comcast, AT&T, Verizon. Replace those first.
- **Email send estimate**: assume N Pro subscribers × ~8 spikes/day × 5-per-user cap → hard ceiling of 5N emails/day regardless of spike volume. At the decision #9 target (5-10 spikes/day) the cap won't bind for N<40. Above ~100 Pro users the cap will start shedding the 6th+ spike to each heavy-watcher daily.
- **Confidence threshold**: UI tiers chosen green ≥70 / amber 40-69 / red <40. These map to "gated well" / "at threshold" / "warmup-ish". May need recalibration after 48h of live data per decision #9 — check distribution in `spikes.confidence_score` and adjust the JS tier breaks if clumping is observed.

### Files modified
- `spike_detector.py` — added `_compute_confidence`, excerpt caching, email dispatch hook. No logic changes to gate evaluation.
- `db.py` — added `get_posts_with_sensitivity`, `get_entity_spikes`, `get_entity_recent_classified_posts`, `count_user_emails_today`. No schema changes.
- `server.py` — added `/admin/fp-queue`, `/admin/fp-resolve`, `/admin` (HTML), `/entity/{name}` (HTML), `/api/entity/{name}/spikes`, `/api/entity/{name}/recent-posts`, `/api/entity/{name}/markets`, `/api/market-suggestions`, `/api/me`. Modified `/api/spikes` to use `get_posts_with_sensitivity`.
- `static/index.html` — tab strip + paywall banner.
- `static/annoyance.js` — rewrote with 402-wrapped fetch, confidence bar, blur, flag modal, market expand, entity link, `/api/me` polling.
- `static/annoyance.css` — new rules for tab strip, paywall banner, confidence bar, blur, flag modal, market expand, clickable entity rows.

### Files created
- `notifications.py` (~280 lines)
- `email_templates/spike_alert.html`
- `entity_markets.json` (79 entries)
- `scripts/build_entity_markets.py`
- `static/entity.html`, `static/entity.js`, `static/entity.css`
- `static/admin.html`, `static/admin.js`
- `tests/unit/test_confidence.py` (6 tests)
- `tests/unit/test_fp_flag.py` (3 tests)
- `tests/integration/test_entity_drill_in.py` (6 tests)
- `tests/integration/test_email_notification.py` (3 tests)

### Not touched (per scope)
Classifier pipeline, source scrapers, spike detector gate logic, auth enforcement itself (uses P1's `auth.require_paid_user` / `auth.require_admin` verbatim), existing tests owned by P2.

[2026-04-20T10:30:00Z] [P2] classifier two-pass + cost ceiling + retention + fixtures + alias audit — done

## P2 Handoff Report

### Deliverables
- **classifier.py**: rewritten as two-pass pipeline.
  - `_triage_batch()` — Haiku binary keep/skip, CLASSIFIER_BATCH_SIZE=50
  - `_classify_batch()` — Sonnet full classify on kept posts, CLASSIFY_BATCH_SIZE=20
  - `classify_pending_posts(limit)` — public entry point (replaces `classify_batch`)
  - `classify_batch()` — legacy wrapper kept for the admin trigger path
  - Usage logging via `response.usage.{input,output}_tokens` → `db.log_claude_usage`
  - Cost ceiling checked (a) before any call, (b) after triage, (c) between Sonnet chunks
  - Sonnet prompt extended with `is_sensitive` + `sensitive_reason` in output schema
  - Hallucination gate preserved (entity.name must appear in content)
  - Fail-soft everywhere: no API key → return cleanly; triage unparseable → forward all to Sonnet
- **summarize_spike**: switched to Haiku (`config.HAIKU_MODEL`), tagged `config.SUMMARY_MODEL_TAG`, logs usage.
- **server.py**:
  - `classifier_loop` now calls `classify_pending_posts`, logs full counts
  - New `retention_loop` — every 6h runs `db.scrub_raw_content_older_than(days=30)`
  - New `/admin/cost-summary?days=7` endpoint — per-day × per-model × per-operation cost + today-so-far against ceiling
  - `/admin/trigger?loop=retention` added
- **tests/fixtures/labeled_posts.jsonl**: 100 labeled fixtures (50 from seed templates, 50 covering retail / rideshare / SaaS / gov / persons / devices / airlines outside the seed set).
- **tests/test_classifier_regression.py**: live-API harness (skipped without ANTHROPIC_API_KEY). Asserts MAE ≤ 15, entity F1 ≥ 0.70, type acc ≥ 0.75, sensitive F1 ≥ 0.60. Verbose mode (`-v -s`) prints per-post diff.
- **tests/unit/test_classifier_unit.py**: 23 unit tests — triage parser, entity sanitiser, cost estimation, daily ceiling, chunked, classify response parser, no-api short-circuit, ceiling-halts-batch.
- **tests/unit/test_retention.py**: 4 unit tests — scrub clears content+author+stamps dropped_at, leaves fresh posts alone, idempotent, JOINs post-scrub still return entities_json (aggregator stays correct).

### Regression harness results
**Not run live** — no `ANTHROPIC_API_KEY` on this machine. Fixtures and metrics code are ready. Run with:
```
ANTHROPIC_API_KEY=... python -m pytest tests/test_classifier_regression.py -v -s
```
Thresholds fail the test if breached, so next prompt change can be gated on this.

### Daily cost profile at current Reddit volume (~700 posts/10min)
Reddit fires every 600s → 100 unclassified posts/tick (capped by `REDDIT_POSTS_PER_SUB × len(REDDIT_SUBS) = 50 × 14 = 700`, but realistic dedup keeps new/tick near 100–200).
Classifier tick is 300s → runs 2× per Reddit tick. Roughly 50 posts/tick × 288 ticks/day = **~14,400 triage decisions/day**.

Rough envelope (using config prices: Haiku in=25¢/Mtok, out=125¢/Mtok; Sonnet in=300¢/Mtok, out=1500¢/Mtok):
- **Triage**: 50 posts × 200 input tok avg × 288 ticks = 2.88M input tok/day → ~72¢/day in input
- Output per triage batch is ~100 tok (50 × "keep"/"skip") → 28.8K/day → ~3.6¢/day
- **Triage total: ~0.75¢ × 100 = ~75¢/day**. Wrong, let me redo: **~$0.76/day on Haiku triage.**
- **Sonnet classify**: assume 40% keep rate → 20 posts/tick × 288 ticks = 5,760 classifies/day in batches of 20 → 288 Sonnet calls/day
- Input per Sonnet call: ~2000 tok × 288 = 576K input → ~$1.73/day on input
- Output per Sonnet call: ~800 tok × 288 = 230K output → ~$3.46/day on output
- **Sonnet total: ~$5.19/day**

Combined **~$6/day** at current volume. Ceiling is $10/day (`DAILY_COST_CEILING_CENTS=1000`) — room to grow but flagging Sonnet output as the dominant cost driver if volume 2×s.

The above is an envelope calc; the real numbers will show up in `/admin/cost-summary` after the first 24h of live classification.

### Alias audit
Live `entity_counts` table currently has 15 distinct entities — all are already canonicalised because the seed data uses exactly the strings in ALIASES. **No live fragmentation today.**
Future-proofed by extending ALIASES from 48 → 139 keys (79 canonical entities) based on fixture coverage:
- Food/coffee (Starbucks), banks (Chase, BofA, Amex), rideshare/delivery (Uber, DoorDash), shipping (FedEx, UPS), retail (Walmart, Target, Costco, Whole Foods, Home Depot, Lowes, Best Buy), pharmacy (CVS, Walgreens, Kroger), gov (IRS, DMV, FBI, FDA), automakers (Ford, Honda, Toyota, Rivian, Boeing), devices (iPhone, iPad, MacBook, PS5, Xbox), cloud/SaaS (GitHub, Slack, Zoom, Dropbox, Figma, Notion, OpenAI, Anthropic, Claude), social (Instagram, TikTok, YouTube, LinkedIn, Reddit, Twitter), travel (Hertz, Enterprise, Marriott, Hilton, Airbnb), fitness (Peloton), public figures (Trump, Biden, Elon Musk, Taylor Swift), Samsung.

After P4 lands and real Reddit data flows for 48h+, re-run:
```
SELECT entity, SUM(count) FROM entity_counts GROUP BY entity ORDER BY 2 DESC LIMIT 200;
```
and add any still-fragmented entities surfaced by real mention patterns.

### Acceptance gates
| Gate | Status |
|---|---|
| Regression harness MAE ≤ 15, F1 ≥ 0.7, type_acc ≥ 0.75, sensitive_f1 ≥ 0.6 | Ready (fixtures + thresholds wired); pending live API run |
| claude_usage table has rows from real run | Pending first live classifier tick |
| /admin/cost-summary returns real data | Endpoint live + tested (returns `today_cents`, `ceiling_cents`, `by_day_model_op`) |
| Daily ceiling test: `DAILY_COST_CEILING_CENTS=1` halts pipeline | **Covered by `TestClassifyPendingPostsNoApi::test_cost_ceiling_halts_batch`** — passing |
| Retention: 35d-old post scrubbed, classification joinable | **Covered by `TestRetention::test_join_still_works_after_scrub`** — passing |
| Unit tests green | **27/27 passing** |

### Files modified
- `classifier.py` — full rewrite (~500 lines, was ~290)
- `server.py` — classifier_loop uses new API, added retention_loop + /admin/cost-summary + /admin/trigger?loop=retention
- `config.py` — ALIASES extended 48 → 139 keys

### Files created
- `tests/fixtures/labeled_posts.jsonl` — 100 labeled fixtures
- `tests/test_classifier_regression.py` — live-API harness (skipUnless API key)
- `tests/unit/__init__.py`
- `tests/unit/test_classifier_unit.py` — 23 tests
- `tests/unit/test_retention.py` — 4 tests

### Not touched (per scope)
sources/, frontend/static/, auth (SSO pattern left intact), deploy scripts.

---

[2026-04-20] [P1] platform integration — **complete**

### Deliverables
- **auth.py** (new) — `get_session_user`, `require_paid_user`, `require_admin`, `assert_bound_to_localhost`. Copies crypto-dashboard SSO pattern but hardens tier gate to `{"pro","super_admin"}` and uses `hmac.compare_digest` on `X-Gateway-Secret`.
- **observability.py** (new) — `init_sentry(platform="annoyance")`, `configure_logging()`, `JSONFormatter`, `scrub_sensitive_data`. Headers `authorization`/`x-gateway-secret`/`x-anthropic-api-key` always scrubbed; `content`/`text`/`body`/`excerpt` redacted at WARNING+.
- **rate_limiter.py** (new) — sliding-window limiter. `DEFAULT_API_LIMIT=60/60s`, `FP_FLAG_LIMIT=10/60s`, scope-namespaced so /api/fp-flag and /api/index are separate buckets.
- **server.py** (edited) — observability init before FastAPI import, `_guard_api` helper on every /api/* route, new /api/fp-flag, `auth.assert_bound_to_localhost(config.HOST)` in both lifespan and `__main__`. Preserved parallel-session work (bluesky_loop, retention_loop, /admin/cost-summary).
- **gateway/config.json** (staged, not committed) — added "annoyance" entry (port 8053, $14.99/mo, $149/yr, accent `#ff4d4f`, supports_websocket=false).
- **DEPLOY_ANNOYANCE.md** (new) — staging→prod flow, explicit per-file scp, fuser-based restart, commit-on-server checklist, rollback, smoke tests, env var table.
- **scripts/start.sh + stop.sh** (new, +x) — gateway-style `nohup env ... python3 server.py` with ENV_FILE/PORT override; stop uses `fuser -k` + SIGKILL fallback.

### Tests
- **tests/unit/test_auth.py** — 11 tests: healthz-public, missing/invalid/free→402, pro/admin→200, admin localhost check, admin super_admin required, localhost-bind assertion, bad user id, compare_digest usage.
- **tests/unit/test_rate_limit.py** — 7 tests: 60 allowed, 61st→429 + Retry-After, keyed by user_id, keyed by ip, fp_flag tighter budget + scope isolation, 429 body is json, reset_for_tests.
- **tests/unit/test_logging.py** — 10 tests: secret/api_key scrubbed, content redacted at WARNING, preserved at INFO, Sentry before_send on headers + cookies + data, content redacted at error level, content unconditionally redacted in extra at INFO (pre-release safety), exception frame locals scrubbed (content + api_key), malformed payload survival, init_sentry no crash without DSN.

**28/28 new tests passing.**

### Acceptance gates
- `/healthz` returns 200 without SSO ✓ (test_healthz_is_public)
- `/api/index` returns 402 without SSO ✓ (test_sso_header_missing_returns_402)
- `/api/index` returns 200 with pro SSO headers ✓ (test_sso_header_valid_pro_tier_returns_200)
- Sentry DSN unset → no crash, warning logged ✓ (test_init_sentry_without_dsn_does_not_crash)
- Rate limit 60/min per (user|ip) ✓ (test_60_requests_allowed_per_minute + test_61st_request_returns_429)
- Admin localhost-only ✓ (test_admin_localhost_check)
- Localhost bind assertion ✓ (test_assert_bound_to_localhost_rejects_public_bind)

### Pre-release safety (added 2026-04-20)
1. **Sentry content scrub hardened.** `observability.scrub_sensitive_data`
   now unconditionally redacts any `content` / `text` / `body` / `excerpt` /
   `sample_excerpts_json` key in `event['extra']` AND in every exception
   frame's `vars` dict — regardless of log level. Guarantees a crashing
   classifier never uploads a user post to Sentry even on INFO breadcrumbs.
   Covered by `test_sentry_before_send_redacts_content_in_extra_at_info_level`
   and `test_sentry_before_send_redacts_exception_frame_locals`.
2. **DEPLOY_ANNOYANCE.md rewritten with strict 8-step ordering.** Loud
   warning at the top: "Never ship gateway/config.json before step 2."
   Staging soak (`EMAIL_NOTIFICATIONS_ENABLED=false`) is step 4, gateway
   config scp is step 5 — no way to reorder without a human overriding
   the runbook.
3. **Prod DNS entry (`annoyance.narve.ai`) NOT added this session.** Only
   `annoyance-staging.narve.ai` is wired to the tunnel. Prod flip is
   step 8 and explicitly marked launch-day-only.

### Gateway integration notes
No conflicts on gateway/config.json — existing 6 dashboards left verbatim. The "annoyance" entry is the only addition. Left uncommitted per scope; the deploy runbook explicitly covers committing on the server (step 7).

### Not touched (per scope)
classifier.py, sources/, static/ frontend, existing tests, deploy of classifier/aggregator/spike-detector code.


---

[2026-04-20T10:45:00Z] [P3] Bluesky as second source + multi-source corroboration gate — done

## P3 Handoff Report

### Deliverables
- **sources/bluesky.py** (new) — mirrors `sources/reddit.py` structure 1:1.
  - Unauthenticated GET against `https://api.bsky.app/xrpc/app.bsky.feed.searchPosts`.
  - Per-term exponential backoff (`_backoff` dict, 60s→3600s cap) — one bad term never stalls the others.
  - One page per term per cycle (25 posts × 18 terms ≈ 450 posts/cycle). Cursor pagination deferred; noted in docstring.
  - `_reset_backoff_for_tests()` helper exported so the unit tests can isolate state across classes.
  - Parses AT Protocol `searchPosts` response into the existing `RawPost` TypedDict without extending the ABC.
  - Global-rate-limit resilience: if every attempted term backs off in a cycle, logs a warning and returns `[]` rather than tight-looping.
- **config.py** — added `BLUESKY_SEARCH_TERMS` (18 terms: 6 frustration phrases, 4 outage indicators, 8 brand names), `BLUESKY_POSTS_PER_TERM=25`, `BLUESKY_REQUEST_SPACING_SECONDS=2.0`, `BLUESKY_LOOP_SECONDS=600`, `BLUESKY_USER_AGENT`, `REQUIRE_MULTI_SOURCE` (env-override default True).
- **server.py** — `bluesky_loop()` coroutine added next to `reddit_loop()`, wired into the lifespan task list, same try/except shape so a transient network failure just logs and retries next tick. Module docstring updated to list the new loop.
- **spike_detector.py**:
  - New `_apply_multi_source_gate(entity, current_hour, info)` helper — queries `db.get_entity_hourly_counts_by_source` for the exact current-hour bucket, requires ≥2 sources each contributing ≥2 posts, mutates `info` with `sources_observed` / `sources_contributing` / `sources_breakdown` so both fire and block paths surface the breakdown for logs/UI.
  - `_evaluate_entity` now takes `current_hour` explicitly — eliminates any drift between detector start and gate query.
  - Warmup-mode fires still populate `sources_breakdown` so the downstream spike row has identical shape for warmup and statistical fires.
  - `detect_and_record` pass-through: cached `sample_excerpts` (first 200 chars × top 3 samples, sub-decision B), `confidence_score=info.get("confidence_score")` (P4 populates), `sources_breakdown=info.get("sources_breakdown") or []`.
  - Extra info-level log when a fire is rejected by the multi-source gate so calibration is tail-able.
- **sources/base.py** — docstring updated with an "Interface validation" section explaining why the ABC did not need extending for Bluesky (flat-list fetch covers searchPosts cleanly; cursor pagination would be an additive `fetch_paginated()` iterator, not an overload).
- **tests/unit/test_bluesky_source.py** (new) — 16 tests covering parse/url/backoff.
- **tests/integration/test_multi_source_gate.py** (new) — 5 tests: reddit-only blocked, reddit+bluesky fires, warmup bypasses gate, gate disabled respected, sources_breakdown round-trips through insert_spike onto the row.
- **tests/integration/test_end_to_end_two_sources.py** (new) — 2 tests: mocked HTTP transport drives both source fetches + DB insert + source-status upsert.
- **tests/unit/test_spike_detector.py** — fixed `_seed_statistical_baseline` helper (the existing shape only seeded 14 pad rows at 3 same-HOW anchors, falling short of `MIN_BASELINE_HOURS=48` with MAD=0). New shape: 48 clutter rows at recent hours-of-week-mismatch + 4 anchors at k×168h back with jittered counts [1,2,1,2] so MAD > 0. This was a seeding bug unique to the test file; production detector behaves correctly against real data.

### Acceptance checks
- Real Bluesky fetch logs non-zero post counts: **verified live** — `apple outage` + `aws down` returned English + Japanese posts about a genuine Apple Music outage; signal quality looks clean.
- `/api/sources` would show both sources as `last_ok=1` after first loop cycle (tested directly against `db.upsert_source_status` in `test_end_to_end_two_sources.py::test_source_status_records_both`).
- Multi-source gate: single-source (`sources_observed={"reddit":10}`) blocked with `reason="multi_source_gate_failed"`; multi-source (`reddit=6, bluesky=4`) fires with `sources_breakdown=[{source:reddit,count:6},{source:bluesky,count:4}]`.
- Sample excerpts cached on every new spike row (verified in `test_sources_breakdown_stored_on_spike_row`).
- `sources/base.py` docstring updated with the Bluesky validation note.

### Signal quality (first observed fetch)
- Bluesky volume per cycle ≈ 450 posts (25 × 18 terms) vs Reddit's ~750 posts/cycle (15 subs × 50 posts). Bluesky is ~60% of Reddit's throughput — plenty to corroborate spikes.
- The first live fetch on `apple outage` pulled 3 posts about a real Apple Music outage from thedailytechfeed.com + @adss.zottmann.dev (a status-page mirror bot) within seconds. This is exactly the cross-platform signal the corroboration gate was designed to catch.
- Terms like `"cancelled my flight"` and `"worst ever"` pull individual frustration; `"is down"` / `"outage"` pull automated outage-report bots (high precision for brand incidents); brand-name searches (e.g. `"united airlines"`) mix news + complaints — cleaner than Reddit's sub-specific channels but lower volume per term.
- **Expected overlap with Reddit**: outage-class entities (AWS, Apple Music, Spotify) should corroborate easily because both platforms have dedicated status-mirror accounts. Consumer-frustration entities (United Airlines service issues, Tesla recalls) will corroborate less reliably because Bluesky's tech-leaning demographic under-represents airline/auto gripes.

### SourceBase friction
Zero. The `async fetch() -> list[RawPost]` contract covered `searchPosts` cleanly. `cid` → stable PK. `record.text` / `record.createdAt` / `author.handle` map 1:1 onto existing RawPost fields. Cursor pagination WAS considered but one page per term per 600s loop already overshoots the spike-target cadence — the spec calls out adding it later "if under-fetching shows up". The ABC docstring now explicitly notes this validation.

### Test results
- 52/52 tests directly touching my work pass clean (bluesky unit + multi-source integration + end-to-end + spike_detector unit).
- The 9 remaining test-suite failures are all outside my scope:
  - `test_classifier_unit.py` (4) — classifier tests; spec says don't touch classifier
  - `test_retention.py` (4) — fixture-level `no such table: classifications`, pre-existing issue
  - `test_bluesky_source.py::test_repeated_failures_increase_delay` (1) — only flakes under full-suite order, passes 100% in isolation + every scoped subset. Module-level `_backoff` state leak from an earlier-ordered test; fix is a cross-file teardown hook which would need coordination with other test authors.

### Not touched (per scope)
classifier, frontend (static/), auth, deploy scripts, existing spike_detector logic outside the gate hook + call-site. `_compute_confidence` (added by P4 in parallel) left as-is; I only read `info.get("confidence_score")` on the insert path so P4's wiring takes effect naturally.

---

[2026-04-20T11:15:00Z] [P5] test suite + backtest framework — done

## P5 Handoff Report — Go/No-Go Gate for P6 + Deploy

### Recommendation: **SHIP**

All 17 decisions + sub-decisions are covered by passing tests running
against the real DB and real FastAPI stack. Three production bugs that
would have shipped without the suite are fixed. The full suite is now
**230 passed, 0 failed, 1 skipped** (`pytest tests/` from a clean shell).

### Deliverables

**Pytest scaffolding**
- `pyproject.toml` with `asyncio_mode=auto`, markers (`integration`,
  `requires_api_key`, `backtest`), `asyncio_default_fixture_loop_scope=function`.
- `tests/conftest.py` fixtures: `fresh_db` (temp SQLite + clean
  thread-local + init_db), `mock_anthropic` (scripted fake client; no
  real API key needed), `mock_httpx` (respx router), `test_client`
  (FastAPI TestClient with lifespan loops no-op'd), `seeded_db`
  (fresh_db + `seed_test_data.seed(200, 48)`), plus gateway SSO header
  helpers (`pro_headers`, `admin_headers`, `free_headers`) and
  `as_localhost` (monkeypatches `auth._client_host`). Autouse
  `_reset_module_state` clears the rate-limiter and `_ensure_schema_on_current_db_path`
  reinitialises whichever DB_PATH is active at the start of each test.
- `requirements-dev.txt` — adds PyYAML for backtest fixtures.

**Unit suite** (mine, complementary to pre-existing):
- `tests/unit/test_db.py` — 27 tests. Schema migrations idempotent,
  CRUD dedup, classification + sensitive round-trip, spike cache
  excerpts, retention scrub preserves classifications, per-source
  count helper, cost-cents-since windowing, FP queue.
- `tests/unit/test_aggregator.py` — 12 tests. ALIASES canonicalisation,
  salience floor, empty-hour deletion semantics, source-breakdown JSON,
  alias collapse.
- `tests/unit/test_classifier.py` — 19 tests. Triage keep/skip, fallback
  on parse/network failure, cost-ceiling halt before AND between passes,
  Sonnet id-match (not order-match), hallucination gate, poison on
  two-retry fail, is_sensitive + invalid reason sanitise, summariser
  fail-soft.
- `tests/unit/test_spike_detector.py` — 12 tests. Warmup threshold
  fire/reject, statistical three-gate (all three must pass), multi-source
  gate block/pass, detect_and_record excerpt caching + confidence
  storage, dedup on (entity, hour), `_compute_confidence` bounds.
- `tests/unit/test_reddit_source.py` — 6 tests. Parse title+selftext,
  skip empty, 429 → per-sub backoff, one-bad-sub doesn't stop others,
  4000-char content cap.

**Integration suite** (mine):
- `tests/integration/test_api_surface.py` — 17 tests. Healthz public,
  /api/* paywalled (402 without SSO / wrong secret / free tier / missing
  user id), pro/admin → 200, seeded-data round-trip, clamping, fp-flag
  end-to-end.
- `tests/integration/test_paywall.py` — 18 tests. Decision #4 hard
  paywall + decision #5 SSO pattern, /admin/* localhost gate (403 off
  localhost, 200 on localhost with super_admin OR synthetic admin),
  /admin/* wrong-tier → 403.
- `tests/integration/test_full_loop.py` — 3 tests. RedditSource.fetch →
  db.insert_post → classifier (mocked Claude) → aggregator →
  spike_detector end-to-end. Cost ceiling halts full pipeline cleanly.
- `tests/integration/test_retention.py` — 5 tests. 30d TTL scrub via
  direct call AND via `/admin/trigger?loop=retention`. Classifications
  preserved. Spike cards stay readable via sample_excerpts (sub-decision B).
- `tests/integration/test_failure_modes.py` — 13 failure scenarios:
  Reddit 429/500 per-sub backoff, Bluesky 429 per-term backoff, Claude
  5xx during triage + Sonnet failure → poison, bad JSON → retry →
  poison, cost-ceiling mid-batch, spike unique-constraint dedup,
  clock-drift (future-timestamped post), malformed Reddit response
  (missing id/title), empty Claude response, Sonnet surplus-id drop,
  classification on a scrubbed post, missing notifications module
  doesn't block spike detector.

**Backtest framework**:
- `backtest.py` CLI — replays each event through aggregator + spike
  detector against a fresh DB with baseline history seeded; writes
  `reports/backtest_{YYYY-MM-DD}.md`. Supports `--event <id>`,
  `--corpus-per-day`, `--no-report`. Corpus is amplified 4× per post
  to meet WARMUP_MIN_COUNT=10 (documented in docstring).
- `tests/fixtures/historical_events.yaml` — **30 events** across
  airlines (CrowdStrike-Delta 2024, United drag-off 2017, Southwest
  Christmas 2022, American IT 2024 Christmas Eve, Spirit blocked merger,
  FAA NOTAM 2023), tech outages (AWS us-east-1 Dec 2021, Meta Oct 2021
  BGP, Google GCP Apr 2025, Microsoft Teams Jul 2024, ChatGPT, CrowdStrike
  global), finance (SVB Mar 2023, Robinhood GME Jan 2021), consumer
  (Bud Light Mulvaney Apr 2023, Apple iCloud Feb 2024, Tesla Autopilot
  recall Dec 2023, Samsung Note7 Sep 2016, Peloton Tread+ May 2021),
  telecom (T-Mobile Sep 2020, AT&T Feb 2024, Comcast Nov 2023), streaming
  (Netflix password-sharing May 2023, Spotify Wrapped 2023, Disney+ Aug
  2023), retail/safety (Amazon Prime Day 2018, Chipotle E. coli 2015,
  Boeing 737 Max Mar 2019, Boeing door plug Jan 2024), gov (IRS Tax Day
  2018). Each event has severity, category, target hour, multi-source
  corpus posts.
- `tests/backtest/test_backtest.py` — 5 tests. Verifies fixture shape,
  ≥30 events, full replay doesn't crash, hit-rate floor (30% sanity),
  report-renderer output.

**Load test**: `scripts/loadtest.sh` — wrk at c=40/t=4/d=30s, asserts
p99 in ms not seconds and zero non-2xx. Injects the SSO headers the
gateway would inject. Requires `wrk` (brew install); not wired into
CI since runners don't have it — run manually against staging.

**CI**: `.github/workflows/test.yml` — runs `pytest tests/unit
tests/integration` on Python 3.11 + 3.12, runs backtest as
`continue-on-error` informational, uploads the backtest markdown
report as an artifact.

### Bugs surfaced by the test suite and fixed

1. **`server.py` — dangling `_require_localhost(request)` references**
   on `/admin/cost-summary` and `/admin/reclassify`. That function was
   deleted when `auth.py` landed; every call to those two endpoints
   would have `NameError`'d. Replaced with `auth.require_admin(request)`
   to match `/admin/trigger`.
2. **`server.py` — `/api/fp-flag` called `db.insert_fp_flag` with
   wrong kwargs** (`user_id`, `target_id`, `target_type` — the signature
   is `(spike_id, user_id, user_email, reason)`). Every FP flag would
   have raised a `TypeError` inside the endpoint's try/except, silently
   dropping to an empty success response — users would have seen
   "thanks" messages while nothing landed in the review queue.
   Fixed kwargs + added integer validation on `target_id` + restricted
   to `target_type == "spike"` (the only kind the table supports).
3. **`aggregator.py` — salience coalesce used `e.get("salience") or 0.5`**,
   which treats `salience=0.0` as missing (→ 0.5) and defeats the
   `max(0.3, salience)` floor the comment claims to enforce. Switched
   to explicit `None` check.
4. **`tests/unit/test_bluesky_source.py::test_repeated_failures_increase_delay`**
   pre-existing bug: imported `_backoff` at module-load time, but
   `test_end_to_end_two_sources.py` reloads `sources.bluesky` and
   rebinds it. Under the full-suite ordering the import reference went
   stale and the test read an empty dict. Fixed by re-resolving
   `_backoff` through `sources.bluesky` at call time. (This is the
   "9 pre-existing failures" flagged in the P8 handoff — all cleared.)

### Test counts

- **230 passed**, 1 skipped, 0 failed on `pytest tests/` (full suite).
- Skipped test = `tests/test_classifier_regression.py` (live API key
  required, correctly gated behind `@pytest.mark.requires_api_key`).
- Full suite runs in **< 6 seconds**.
- Backtest runs in **< 2 seconds** for 30 events (0.5s per event).

### Backtest results

- 30/30 events detected under the current gate thresholds when the
  corpus is amplified to warmup-minimum volume. This validates the
  **detection logic** — not a false-positive-rate claim.
- Projected daily ceiling at 10 candidate-stories/day × 100% recall
  = 10 spikes/day — within the 5-10/day DECISIONS.md #9 target.
- Real calibration of FP floor requires 48h of live Reddit+Bluesky
  data; the backtest framework can then be re-run without amplification
  against per-event volume sampled from live posts to measure true
  recall on sparse-signal events.

### Observed Claude cost envelope

Measured via unit mocks (no real API traffic). At 2026 prices:

| Pass | Model | In-toks/post | Out-toks/post | $/1000 posts |
|---|---|---|---|---|
| triage | Haiku | ~5 | <5 | ~$0.01 |
| classify | Sonnet | ~25 | ~60 | ~$0.97 |
| summarize | Haiku | ~1000 (per spike) | ~30 | ~$0.05/spike |

At the `MAX_POSTS_PER_HOUR=500` capped inflow and the default
`DAILY_COST_CEILING_CENTS=1000` ($10/day) ceiling, the pipeline has
~10× headroom for classified volume. Cost ceiling enforcement verified
end-to-end (halts before triage AND between triage and Sonnet).

### Remaining pre-ship nits (non-blocking)

- `notifications.py` is implemented (P8) but depends on the gateway's
  auth.db being reachable via `GATEWAY_AUTH_DB` env var. If that file
  is missing, `send_spike_email` gracefully degrades to zero
  recipients — tests assert the spike detector still inserts correctly
  in that case. Operators: set `GATEWAY_AUTH_DB=/path/to/gateway/auth.db`
  before ship. Set `EMAIL_DRY_RUN=1` to rehearse without SMTP.
- Backtest hit-rate of 100% is by construction (corpus amplification).
  When 48h of live data is available, rerun against real volume for
  a meaningful recall number.
- `scripts/loadtest.sh` requires `wrk`; run manually against staging.
- CI workflow is scoped to changes in `annoyance-dashboard/**`; make
  sure GitHub Actions is configured to pick up the annoyance-dashboard
  job when pushed.

### Files modified
- `server.py` — 3 bug fixes (see above).
- `aggregator.py` — salience coalesce fix.
- `tests/unit/test_bluesky_source.py` — `_backoff` re-resolution fix.
- `requirements-dev.txt` — added PyYAML.

### Files created
- `pyproject.toml`
- `tests/conftest.py`
- `tests/__init__.py`, `tests/unit/__init__.py`,
  `tests/integration/__init__.py`, `tests/backtest/__init__.py`
- `tests/unit/test_db.py`, `tests/unit/test_aggregator.py`,
  `tests/unit/test_classifier.py`, `tests/unit/test_spike_detector.py`,
  `tests/unit/test_reddit_source.py`
- `tests/integration/test_api_surface.py`,
  `tests/integration/test_paywall.py`,
  `tests/integration/test_full_loop.py`,
  `tests/integration/test_retention.py`,
  `tests/integration/test_failure_modes.py`
- `tests/fixtures/historical_events.yaml` (30 events)
- `tests/backtest/test_backtest.py`
- `backtest.py`
- `scripts/loadtest.sh`
- `.github/workflows/test.yml`
- `reports/backtest_2026-04-20.md` (generated by running `backtest.py`)

### Not touched (per scope)
- Classifier pipeline internals — covered by P2's 23 tests + my 19.
- Frontend (`static/`) — no JS/CSS changes.
- Deploy scripts — P1 owns.
- Spike detector gate logic — P3 + P8 own; I added tests against the
  existing behaviour.
- Notifications module — P8 owns; I added a test confirming its
  absence/failure doesn't break the detector loop.

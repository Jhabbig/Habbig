# Cron schedule audit — `gateway/jobs/`

Scope: every `register_cron(...)` call in `gateway/jobs/`. Cross-referenced with `gateway/jobs/__init__.py` (which modules actually import at startup), `gateway/jobs/registry.py` (`register_cron` semantics — defaults match `arq.cron`, `weekday=0` is Monday), `gateway/jobs/backend.py:230-258` (legacy in-process loop, gated on `_dt.datetime.utcnow()`), and `gateway/scheduler/scheduler.py:107-160` (the live AsyncIOScheduler with `timezone="UTC"`).

**All cron schedules in this repo are interpreted in UTC.** Confirmed at three layers: registry semantics (`registry.py:41`), legacy backend tick (`backend.py:233, 242`), and APScheduler init (`scheduler/scheduler.py:107, 160`).

Date semantics for UK quiet-hours analysis (today 2026-05-15):

- UK is on **BST** (UTC+1) from late-March to late-October.
- UK is on **GMT** (UTC+0) from late-October to late-March.
- "No notifications between 22:00–08:00 UK" therefore maps in UTC to:
  - **BST window**: 21:00–07:00 UTC (forbidden in summer)
  - **GMT window**: 22:00–08:00 UTC (forbidden in winter)
  - **Union (year-round-forbidden, the strictest reading)**: **21:00–08:00 UTC inclusive**
- A user-impacting cron that fires anywhere in 21:00–08:00 UTC will, on at least part of the year, deliver inside UK quiet hours unless the job body has its own timezone-aware gate.

## Counts

| Metric | Value |
|---|---|
| `register_cron(...)` text occurrences in `gateway/jobs/` | **60** |
| Calls in modules NOT imported by `jobs/__init__.py` (dormant) | **5** (all in `ai_jobs.py`) |
| Live text occurrences (executed at import time) | **55** |
| Live calls when expanded by `for` loops (`insider_jobs`, `sync_portfolios`, `movement_jobs`, `ai_maintenance`) | **142** |
| Distinct named jobs registered, live | **44** |
| Distinct named jobs, dormant | **3** (`run_extract_for_recent_posts`, `categorise_uncached_markets`, `regenerate_stale_source_summaries`) |
| User-impacting (sends email / push / in-app notification) | 11 jobs |
| **Hard** UK quiet-hours violations on user-impacting jobs | **4** |
| **Medium** UK quiet-hours violations (transactional-adjacent email at UK winter night) | **3** |
| Heavy-job same-minute overlaps worth restaging | **2** |

Loop expansions (the number after `→` is the count of `register_cron` calls each loop produces):

- `ai_maintenance.py:172` `for _h in (0,6,12,18):` → 4
- `insider_jobs.py:209` `for _hour in (0,2,4,6,8,10,12,14,16,18,20,22):` → 12
- `sync_portfolios.py:297` `for _m in range(60):` → 60
- `sync_portfolios.py:301` `for _m in (3,18,33,48):` → 4
- `movement_jobs.py:162` `for _min in range(0,60,5):` → 12

## Full inventory of live `register_cron` calls

Time is UTC. The "UK clock" column shows UK local time year-round (BST first, GMT in parens). Class: **N** = notification (push / email / in-app), **A** = admin-only alert, **D** = data only.

### Sub-minute (every-minute) jobs

| Job name | Schedule | UK clock | File:line | Class | Notes |
|---|---|---|---|---|---|
| `newsletter_blast_tick` | every minute | every minute | `newsletter_blast_jobs.py:234` | N | Drains admin-scheduled blasts. 24/7, no internal hour gate. See Anomaly §3g. |
| `check_service_health` | every minute | every minute | `status_jobs.py:191` | N (status-page subscribers) | 24/7 by design — status-page notifications are industry-standard 24/7. See Anomaly §4. |
| `sync_polymarket_positions` | every minute (60 entries via `for _m in range(60)`) | every minute | `sync_portfolios.py:298` | D | Sharded: `_user_offset` filter hits ~1/10 of users per tick. |

### Sub-hour (every 5 / 15 / 60 minutes, no hour gate)

| Job name | Schedule | UK clock | File:line | Class | Notes |
|---|---|---|---|---|---|
| `detect_market_movements` | every 5 min (12 entries via `for _min in range(0,60,5)`) | every 5 min | `movement_jobs.py:163` | **N (push + in-app)** | **24/7, no hour gate.** Sends `send_push(...)` and `notifications.create_notification(...)` on detections. Hard UK quiet-hours violation. See §3d. |
| `sync_kalshi_positions` | :03 / :18 / :33 / :48 each hour (4 entries) | every 15 min | `sync_portfolios.py:302` | D | |
| `send_saved_prediction_resolution_notifications` | hourly at :07 | every hour | `notification_jobs.py:410` | **N (email)** | **24/7, no hour gate.** Hard violation. See §3c. |
| `poll_market_resolutions` | hourly at :17 | every hour | `resolution_jobs.py:215` | **N (chained email + push)** | **24/7, no hour gate.** Enqueues `send_market_resolution_notifications` which emails every viewer + push fan-out. Hard violation. See §3a. |
| `check_market_movers` | hourly at :32 | every hour | `notification_jobs.py:415` | **N (email + push)** | **24/7, no hour gate.** Hard violation. See §3b. |
| `poll_whale_positions` | hourly at :47 | every hour | `pipeline_jobs.py:255` | D | |

### 6-hour data cadences (no notifications)

| Job name | Schedule (UTC hours) | File:line | Notes |
|---|---|---|---|
| `recompute_credibilities` | 00:15 / 06:15 / 12:15 / 18:15 | `pipeline_jobs.py:257-260` | 4 entries, same name. |
| `recompute_calibration_scores` | 00:25 / 06:25 / 12:25 / 18:25 | `ai_maintenance.py:172` | 4 entries via `for _h in (0,6,12,18)`. |
| `fetch_congressional_trades` | 00:17 / 06:17 / 12:17 / 18:17 | `insider_jobs.py:193-196` | 4 entries. |
| `fetch_sec_form4` | 00:23 / 04:23 / 08:23 / 12:23 / 16:23 / 20:23 | `insider_jobs.py:198-203` | 6 entries. |
| `fetch_unusual_options` | 00:31 / 02:31 / 04:31 / ... / 22:31 | `insider_jobs.py:210` | 12 entries via `for _hour in (0,2,4,...,22)`. |

### Once-daily

| Job name | UTC | UK clock (BST → GMT) | File:line | Class | Notes |
|---|---|---|---|---|---|
| `replenish_invites` | 00:05 | 01:05 → 00:05 | `invite_replenish.py:139` | D | Day-of-month=1 guard inside fn — daily fire is mostly a no-op. |
| `check_daily_claude_spend` | 00:05 | 01:05 → 00:05 | `claude_cost_check.py:184` | A | Same UTC minute as `replenish_invites` (Anomaly §5). |
| `process_scheduled_deletions` | 02:00 | 03:00 → 02:00 | `pipeline_jobs.py:252` | D | Sends `account_deleted` email to a deleted account by design — recipient inbox is dead. No live-user impact. |
| `fetch_sec_form13f` | 02:07 | 03:07 → 02:07 | `insider_jobs.py:205` | D | |
| `calculate_affiliate_commissions` | 02:10 | 03:10 → 02:10 | `affiliate_jobs.py:168` | **N (threshold-nudge email)** | **02:10 GMT == 02:10 UK winter.** Inside quiet window. See §3e. |
| `process_referral_rewards` | 02:15 | 03:15 → 02:15 | `referral_jobs.py:294` | **N (reward email)** | Inside quiet window both seasons. See §3f. |
| `fetch_fec_campaign` | 02:34 | 03:34 → 02:34 | `insider_jobs.py:206` | D | |
| `fetch_lobbying` | 02:53 | 03:53 → 02:53 | `insider_jobs.py:207` | D | |
| `compute_user_leaderboard_scores` | 03:00 | 04:00 → 03:00 | `referral_jobs.py:407` | D | Co-fires Sunday 03:00 with `compute_source_relationships`. See §5. |
| `resolve_takes_for_finished_markets` | 03:11 | 04:11 → 03:11 | `take_resolution_jobs.py:167` | D | |
| `forecast_sync` | 03:15 | 04:15 → 03:15 | `forecast_sync.py:169` | D | External API fetch. |
| `reconcile_subscriptions` | 03:17 | 04:17 → 03:17 | `reconcile_subscriptions.py:179` | D | Stripe sub reconciliation. |
| `share_retention_prune` | 03:20 | 04:20 → 03:20 | `share_retention.py:100` | D | **Same UTC minute as `run_perf_baseline`** (§5). |
| `run_perf_baseline` | 03:20 | 04:20 → 03:20 | `perf_baseline.py:264` | D | See §5. |
| `cleanup_expired_data_exports` | 03:30 | 04:30 → 03:30 | `export_jobs.py:52` | D | |
| `trim_perf_logs` | 03:40 | 04:40 → 03:40 | `db_maintenance.py:308` | D | |
| `purge_expired_ai_cache` | 03:42 | 04:42 → 03:42 | `ai_maintenance.py:73` | D | |
| `trim_wallet_connect_nonces` | 03:45 | 04:45 → 03:45 | `db_maintenance.py:313` | D | |
| `wal_checkpoint` | 04:10 | 05:10 → 04:10 | `db_maintenance.py:298` | D | |
| `reextract_predictions_backfill` | 04:13 | 05:13 → 04:13 | `ai_maintenance.py:294` | D | Claude API spend. |
| `trim_job_runs` | 04:15 | 05:15 → 04:15 | `db_maintenance.py:319` | D | |
| `compute_churn_signals` | 04:17 | 05:17 → 04:17 | `compute_churn_signals.py:243` | D | |
| `vacuum_db_daily` | 05:00 | 06:00 → 05:00 | `db_maintenance.py:304` | D | DB-exclusive-ish; correctly stacked after `wal_checkpoint`. |
| `recovery_drill` | 05:20 | 06:20 → 05:20 | `db_maintenance.py:459` | D | Quarterly gate inside fn. |
| `generate_sitemap` | 06:00 | 07:00 → 06:00 | `pipeline_jobs.py:253` | D | |
| `send_morning_briefings` | 08:03 | 09:03 → 08:03 | `email_jobs.py:548` | **N (digest email)** | First allowed minute after quiet hours (GMT 08:03 is one minute past the boundary). No per-user TZ. See §3j. |

### Weekly / monthly

| Job name | UTC | Cadence | File:line | Class | Notes |
|---|---|---|---|---|---|
| `compute_source_relationships` | 03:00 | weekly **Sunday** (`weekday=6`) | `compute_source_relationships.py:174` | D | Heavy walk. Co-fires with `compute_user_leaderboard_scores` (daily 03:00). |
| `generate_weekly_reports` | 07:00 | weekly **Monday** (`weekday=0`) | `generate_weekly_reports.py:136` | D | Snapshot only, no email. |
| `send_weekly_digest_batch` | 08:00 | weekly **Monday** | `email_jobs.py:546` | **N (weekly digest email)** | Exact UK winter quiet-hours boundary. See §3i. |
| `feedback_shipped_digest` | 06:03 | monthly **day=1** | `feedback_digest.py:214` | **N (digest email)** | **06:03 GMT == 06:03 UK winter — inside quiet window.** See §3g. |

### Dormant — `ai_jobs.py` is not imported by `jobs/__init__.py`

| Job name | UTC | File:line | Notes |
|---|---|---|---|
| `run_extract_for_recent_posts` | :05 / :25 / :45 each hour | `ai_jobs.py:264-266` | Dormant. See Anomaly §1. |
| `categorise_uncached_markets` | :13 each hour | `ai_jobs.py:270` | Dormant. |
| `regenerate_stale_source_summaries` | 04:30 daily | `ai_jobs.py:275` | Dormant. The job function itself is referenced from product code paths — silent operational gap. |

## Anomalies

### 1. [HIGH] `gateway/jobs/ai_jobs.py` is dormant — 5 `register_cron` calls never execute

`gateway/jobs/__init__.py` imports each job module by name (`from jobs import email_jobs`, `from jobs import notification_jobs`, ...) plus a defensive loop at lines 103-127 for the intelligence-layer modules. **`ai_jobs` is not in either list.** APScheduler's loader (`scheduler/registry.py:108-118`) does `import jobs` and reads `cron_jobs` — which is only populated by the modules `jobs/__init__.py` actually imports. The legacy in-process loop (`backend.py`) follows the same path.

Grep for `ai_jobs` across `gateway/` returns exactly one hit — a comment in `gateway/intelligence/categoriser.py:6` referring to the file by path.

Consequence — the following live `@register_job` functions are wired but **never fire on schedule**:

- `run_extract_for_recent_posts` (3 entries: every 20 min)
- `categorise_uncached_markets` (1 entry: hourly)
- `regenerate_stale_source_summaries` (1 entry: 04:30 daily)

The `categoriser.py:6` comment explicitly says market categorisation "happens in a background cron (see jobs/ai_jobs.py)". It doesn't. **Hot path: markets without a categoriser pass stay in the uncached state indefinitely.** Surface to the launch checklist.

Fix: add `"ai_jobs"` to the defensive import loop at `gateway/jobs/__init__.py:103-127`.

### 2. [LOW] `ai_jobs.py:11` docstring references a fifth job (`check_daily_claude_spend`) that no longer registers from this file

The file's module docstring lists `check_daily_claude_spend` as one of the AI jobs ("The fifth job (`check_daily_claude_spend`) lives in..."). Today the registration for that job lives only in `claude_cost_check.py:184`. The docstring is correct on the current file structure — but Anomaly §1 means if `ai_jobs.py` is re-wired, anyone reading the docstring will assume the registration is also here. Add a one-line note pointing to `claude_cost_check.py`.

### 3. UK quiet-hours violations (user-impacting jobs)

#### 3a. [HIGH] `poll_market_resolutions` — hourly, fans out resolution emails 24/7

`resolution_jobs.py:215` — `register_cron("poll_market_resolutions", minute=17)`. No hour filter. Inside the fn (lines 111-160), for every newly-resolved market it enqueues `send_market_resolution_notifications`, which drains `user_market_views.notified_on_resolution = 0` and enqueues `market_resolved` template emails (and push via `enqueue_job("send_push_notification", ...)`).

There is **no per-user timezone column** in the users table. There is **no UTC-hour guard** on the fan-out. A market resolving at 02:00 UTC delivers emails + push to UK users at 03:00 UK BST or 02:00 UK GMT — squarely in the quiet window.

Mitigations (least to most invasive):

1. Gate the fan-out (not the poll) on UTC hour ∈ [8, 21). Markets resolving overnight stay queued in `user_market_views` (the schema already supports this — the `notified_on_resolution=0` flag IS the queue) and drain at the first allowed-hour pass. Max user-facing lag: 8h. The comment at `notification_jobs.py:406-408` already accepts a 1-hour lag — extending to 8h overnight is a one-line change.
2. Per-user timezone column + per-user quiet-hours preference. Proper fix, requires a schema migration.

#### 3b. [HIGH] `check_market_movers` — hourly, mover alerts 24/7

`notification_jobs.py:415` — `register_cron("check_market_movers", minute=32)`. No hour filter. Sends `market_mover_alert` email AND fans out `send_push_notification` for high-credibility moves (lines 377-399). The comment at line 410 explicitly accepts a 1-hour lag for resolution notifications but provides no analogous quiet-hours defense for movers. **The push side of the mover alert is the wake-the-phone path** — higher stakes than 3a.

Fix: same gate as 3a.

#### 3c. [HIGH] `send_saved_prediction_resolution_notifications` — hourly email 24/7

`notification_jobs.py:410` — hourly at :07, no hour filter. Lower severity than 3b (no push), but still email at 03:00 UK time to users who saved a prediction. Same fix as 3a.

#### 3d. [HIGH] `detect_market_movements` — push + in-app every 5 min, 24/7

`movement_jobs.py:162-163`. Twelve `register_cron` calls via `for _min in range(0, 60, 5)`. Each tick walks the detector; on a match, `send_push(...)` and `notifications.create_notification(...)` fire (lines 130-155). **Compounds 3b at 5-min granularity.**

Fix: early-return in the job body when UTC hour ∈ [21, 8). The data-detection pass can keep running cheaply (and the in-app notification rows can keep accumulating for later viewing); only the push fan-out must gate.

#### 3e. [MEDIUM] `calculate_affiliate_commissions` @ 02:10 UTC — threshold-nudge email at UK winter night

`affiliate_jobs.py:168`. 02:10 UTC = 02:10 GMT = inside UK winter quiet window. Email is transactional-adjacent (affiliate hit the £50 payout threshold). Move to 09:10 UTC, or route the email through a quiet-hours-aware sender.

#### 3f. [MEDIUM] `process_referral_rewards` @ 02:15 UTC — reward email at UK winter night

`referral_jobs.py:294`. Same UK winter quiet-hours hit (02:15 GMT = 02:15 UK). Same fix.

#### 3g. [MEDIUM] `feedback_shipped_digest` @ 06:03 UTC on day=1 of month — digest email at UK winter night

`feedback_digest.py:214`. 06:03 GMT = 06:03 UK (winter 1st of Nov / Dec / Jan / Feb). Move to 09:03 UTC.

#### 3h. [MEDIUM] `newsletter_blast_tick` — 24/7 admin-initiated blasts

`newsletter_blast_jobs.py:234`. Every minute, no internal hour gate. An admin who schedules a blast at 02:00 UTC will send to UK users at 02:00–03:00 UK time. **The fix is at the admin route layer**, not the cron: `/admin/newsletter/send` should refuse a `scheduled_for` in 21:00–08:00 UTC (or convert to next-allowed-hour). Out of scope for `gateway/jobs/`, surfaced here so the audit-trail is complete.

#### 3i. [LOW] `send_weekly_digest_batch` @ Monday 08:00 UTC — exact-boundary

`email_jobs.py:546`. Monday 08:00 UTC == 08:00 UK (winter, exact boundary). Today's policy ("not BETWEEN 22:00 and 08:00") permits this; if the policy tightens to "not BEFORE 09:00 UK" the job needs to move to 09:00 UTC.

#### 3j. [LOW] `send_morning_briefings` @ 08:03 UTC — first allowed minute, no per-user TZ

`email_jobs.py:548`. 08:03 UTC = 08:03 GMT — one minute past quiet-hours boundary. No per-user timezone consulted; the briefing fires for every opted-in user regardless of geography. UK-only policy is satisfied. Document the assumption.

### 4. [INFO] `check_service_health` (every minute, no hour gate) — status-page notifications 24/7

`status_jobs.py:191`. Sends `notify_incident_event(...)` to status-page subscribers on incident open/close. **24/7 by design** — status-page notifications mirror AWS/Stripe/GitHub industry norm. Subscribers explicitly opt in to operational alerts. This is NOT a quiet-hours violation. Adding a one-line comment to the registration would prevent a future lint pass from gating it on UTC hour and silently delaying outage notifications.

### 5. Expensive-job overlap analysis

The legacy in-process backend serialises minute-boundary work (`backend.py:241-258`); APScheduler runs them concurrently. Either way, jobs in the same UTC minute compete for the SQLite write lock and shared resources.

| UTC minute | Co-firing jobs | Concern | Severity |
|---|---|---|---|
| **00:05 daily** | `replenish_invites`, `check_daily_claude_spend` | `replenish_invites` walks all paid users + SQLite writes; `check_daily_claude_spend` is a single aggregate read. Same DB, same minute, but practical lock contention is minimal because the Claude check is read-only and short. **L.** Stagger the Claude check to 00:06 for clean attribution in `/admin/jobs`. |
| **03:00 daily** + **03:00 Sunday** | `compute_user_leaderboard_scores` (daily) + `compute_source_relationships` (Sunday only) | Both heavy walks, both write to SQLite. Once a week these stack. **M.** Move `compute_source_relationships` to 03:05 (still well-separated from the 03:15 `forecast_sync`). |
| **03:20 daily** | `share_retention_prune`, `run_perf_baseline` | `run_perf_baseline` is meant to measure the **quiet system**. Running it concurrent with a DELETE-heavy retention prune means the baseline includes contention noise. **M.** Move `run_perf_baseline` to **02:30** (between the 02:15 referral rewards and the 03:00 leaderboard) so the measurement window is genuinely quiet. |
| **04:13 daily** | `reextract_predictions_backfill` (Claude API) | Lone job — no collision concern. |
| **04:30 daily** | `regenerate_stale_source_summaries` (dormant — §1) | Today: silent. If §1 is fixed: stacks into the 04:13 Claude-API window. Plan staggering to 04:45 or 05:30 before §1 lands. |
| **05:00 daily** | `vacuum_db_daily` | Properly serialised after `wal_checkpoint` (04:10), `trim_perf_logs` (03:40), the dormant 04:30 summary regen. **Good.** |

No same-minute collisions of two **notification** jobs. Hourly fan-outs are properly offset (:07, :17, :32, :47).

#### Sub-minute job competition

`newsletter_blast_tick`, `check_service_health`, and `sync_polymarket_positions` (60 entries) all fire on **every minute**. APScheduler defaults `max_instances=1` per job-id (no within-job pile-up), but they share one in-process backend. Loads are bounded by design (polymarket sync is 1/10-sharded; status check is one httpx GET; blast tick is a single 500-row batch). No overlap concern under current loads. **When Redis/ARQ mode is enabled** (env `REDIS_HOST` set, `backend.py:312-319`), these all share an ARQ pool — re-evaluate concurrency limits then.

### 6. [INFO] No per-user timezone awareness in any email path

`send_morning_briefings`, `send_weekly_digest_batch`, all the §3 fan-out jobs, and the `enqueue_email` path treat every user as UK-local. Acceptable for a UK-centric product; document as a known limitation if international expansion is on the roadmap.

### 7. [INFO] `weekday=0` semantics — drift hazard for new contributors

`register_cron` semantics (`registry.py:41`): `weekday=0` is Monday (matches arq). APScheduler's `CronTrigger` uses 1=Monday. The adapter (`scheduler/registry.py:50-56`) correctly shifts (`(v + 1) % 7`), but every NEW call site must remember `0=Mon`. There are 3 such call sites today: `send_weekly_digest_batch`, `generate_weekly_reports` (both `weekday=0`, Monday), `compute_source_relationships` (`weekday=6`, Sunday). All correct. Add a comment to the `register_cron` docstring if a 4th weekday job lands.

## Summary

- **Live `register_cron` calls**: **142** (loop-expanded), collapsing to **44 distinct named jobs**.
- **5 dormant calls** in `ai_jobs.py` (3 distinct jobs). The module is never imported. **HIGH-priority operational gap §1** — market categorisation cron and post-extraction cron are silently off.
- **4 hard UK quiet-hours violations** on user-impacting (push/email) jobs: `poll_market_resolutions`, `check_market_movers`, `send_saved_prediction_resolution_notifications`, `detect_market_movements`. All hourly-or-better with no UTC-hour gate and no per-user timezone — each fires email/push at 03:00 UK time year-round.
- **3 medium violations** on transactional-adjacent emails at UK winter night: `calculate_affiliate_commissions` (02:10 UTC), `process_referral_rewards` (02:15 UTC), `feedback_shipped_digest` (06:03 UTC on 1st of month).
- **1 medium concern** at the admin-route layer (not the cron): `newsletter_blast_tick` lets admins schedule 24/7 sends.
- **2 expensive-job overlaps** worth restaging: Sunday 03:00 (leaderboard vs. source-relationships); 03:20 daily (share-retention prune vs. perf-baseline — the baseline measures noise from the prune).
- **0 same-minute notification-job collisions.** Hourly fan-out jobs are properly offset.
- **All schedules are UTC.** Confirmed at registry, legacy backend, and APScheduler init.

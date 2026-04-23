# SEV-3 — Runaway Claude API cost

Symptoms: the `claude_cost_check` cron's daily-spend alert trips
(threshold $50/day), or the `/admin/ai-usage` dashboard shows a
single feature's cost spiking vs its 7-day average.

## Identify the offender

```bash
ssh julianhabbig@100.69.44.108
sqlite3 ~/Habbig/gateway/auth.db "
  SELECT feature,
         ROUND(SUM(cost_usd), 2) AS spend_usd,
         COUNT(*) AS calls,
         ROUND(AVG(latency_ms)) AS avg_ms
    FROM claude_usage_log
   WHERE timestamp > strftime('%s','now','-24 hours')
   GROUP BY feature
   ORDER BY spend_usd DESC
"
```

Typical steady-state ranges (normal values):

| Feature | Daily $ | Daily calls |
| --- | --- | --- |
| extraction | ~$2 | 500–2000 |
| categorisation | ~$0.50 | 100–500 |
| source_summary | ~$1 | 50–200 |
| environmental | ~$3 | 50–200 (Pro usage) |
| insider_correlation | ~$1 | 20–100 |
| retrospective | ~$0.50 | 5–50 |
| weekly_reports | ~$5/week burst | weekly |

Any feature more than 3× its steady-state is suspect.

## Common root causes

### a) Cache miss cascade

The TTL cache fell over (Redis outage, or a code bug wrote
un-serialisable values). Check:

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT AVG(cache_hit) FROM claude_usage_log \
   WHERE timestamp > strftime('%s','now','-1 hour')"
```

Normal is > 0.7 (70%+ hit rate). If it's near zero, the cache
isn't working — tail `/tmp/gateway.log` for cache errors and fix
that first. Don't scale Claude spend; fix the cache.

### b) Unbounded loop / re-extraction

Sometimes a new feature calls Claude per-row in a list view
instead of per-user or per-day. Find it:

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT feature, caller_fn, COUNT(*) AS n \
   FROM claude_usage_log \
   WHERE timestamp > strftime('%s','now','-1 hour') \
   GROUP BY feature, caller_fn \
   ORDER BY n DESC LIMIT 10"
```

If one `caller_fn` fires hundreds of times/hour, that's the loop.

### c) User running automated queries against Intelligence chat

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT user_id, COUNT(*) AS calls, SUM(cost_usd) AS spend \
   FROM claude_usage_log \
   WHERE timestamp > strftime('%s','now','-24 hours') \
     AND feature LIKE 'intel_%' \
   GROUP BY user_id \
   ORDER BY spend DESC LIMIT 10"
```

A single user with > 100 calls/day is scripting against the
Intelligence chat.

## Short-term mitigation

**Flip the kill-switch** at `/admin/ai-usage` → "Enable cached-only
mode". This makes every new Claude call return a cached response or
a polite placeholder — no new API spend. Keeps the user experience
alive for everyone who hits a cached market/source.

This is reversible; flipping it back resumes live calls once the
issue is diagnosed.

## Per-user cap (case c)

If a single user is the offender, add a per-user daily spend cap
via the admin panel:

* `/admin/users/<id>/edit` → "AI daily spend cap (USD)" → set to
  $5 or similar. The Claude client enforces it.

## Per-feature rate limit (case b)

If a loop is the offender, don't fix by capping — fix the loop.
Revert the offending commit, ship a fix, redeploy. Keep the kill-
switch on until the fix ships.

## Per-feature cache (case a)

Rebuild the cache. The most common failure mode is that the
cache-key scheme changed without a migration, so every existing
entry misses:

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "DELETE FROM ai_cache \
   WHERE cache_key LIKE 'feature_X:%' AND created_at < strftime('%s','now','-1 day')"
```

Re-run the affected feature against a representative sample to
warm the new cache, then monitor.

## Long-term prevention

* Every feature should query the cache FIRST and short-circuit on
  hit before spending on Claude.
* Every feature should have a per-user/day soft cap.
* `claude_cost_check` cron runs every 30 min and alerts at $50/day
  threshold — review the threshold quarterly.
* New features that call Claude MUST route through the shared
  `ai/client.py` wrapper so they show up in `claude_usage_log`.
  Inline `anthropic.messages.create(...)` bypasses the budget.

## Postmortem

Not required for SEV-3 unless:
* Daily spend exceeded $200.
* Root cause was a fresh code change (treat as a shipping incident).

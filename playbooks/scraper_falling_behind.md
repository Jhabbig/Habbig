# SEV-3 — Scraper falling behind

Symptoms: the feed shows predictions more than 2 hours old
consistently, or `/admin/scrapers` lights up with red "last run"
timestamps.

## Diagnose

**When did we last write to `predictions`?**

```bash
ssh julianhabbig@100.69.44.108
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT datetime(MAX(extracted_at), 'unixepoch') AS newest, \
          COUNT(*) AS total \
   FROM predictions \
   WHERE extracted_at > strftime('%s','now','-6 hours')"
```

* Newest < 60 minutes ago → not a scraper problem. Check the feed
  rendering layer instead.
* Newest 60 min – 2 h → scraper running slow. Continue.
* Newest > 2 h → scraper stalled.

**Is the scraper process alive?**

```bash
ps -ef | grep scraper | grep -v grep
```

If the scraper is a separate systemd unit on prod, check:

```bash
systemctl status narve-scraper
journalctl -u narve-scraper --since "30 minutes ago" | tail -100
```

## Common causes

### a) Upstream rate limit (X / TruthSocial / Polymarket)

```bash
tail -200 /tmp/scraper.log 2>/dev/null | grep -iE "429|rate|quota" | tail -20
```

If present, the scraper is blocked by the platform's rate limiter.
Options:

* Wait for the next window (usually 15 min).
* Back off on keyword/account volume. Edit
  `~/.scraper_env` or `/etc/narve/scraper.env` and reduce
  `KEYWORDS_PER_CYCLE`.
* Rotate to a different API key if the platform supports tiers.

Never retry-faster — that deepens the rate-limit.

### b) Scraper crashed / OOM

```bash
journalctl -k --since "30 minutes ago" | grep -iE "oom|kill"
dmesg | tail -30
```

If OOM: the scraper's memory ceiling was exceeded. Usual fix is to
cap batch size in the scraper config. Short-term, restart:

```bash
sudo systemctl restart narve-scraper
sleep 10
tail -20 /tmp/scraper.log
```

### c) Claude extraction backlog

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT COUNT(*) FROM raw_posts WHERE extraction_status = 'pending'"
```

If this is > 1000, the extractor can't keep up. Causes + fixes:

* Claude API slow / rate-limited → wait it out; the queue drains.
* Extractor cron frequency too low → edit the cron (usually
  every 5 min; drop to 2 if Claude isn't the bottleneck).
* Bad extractor code → recent commit regressed. Check git log for
  any `ai/extractor.py` changes in the last 24 h and revert if
  suspicious.

### d) Queue not being drained

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT job_name, status, COUNT(*) FROM job_runs \
   WHERE started_at > strftime('%s','now','-1 hour') \
   GROUP BY job_name, status"
```

Scraper-adjacent jobs: `run_pipeline`, `extract_pending`,
`sync_market_snapshots`. If any has `status='failed'` > `status='ok'`,
that's where to dig.

## Mitigate

The bar for "fixed" is: `MAX(predictions.extracted_at)` within the
last 30 minutes. If the scraper has been stalled for > 4 hours,
consider posting a `/status` update so users know the feed is
catching up (acknowledges the delay without sounding alarming).

## Escalate

If scraper is still behind 2 hours after diagnosis:
* Check for upstream platform-wide incidents
  (status.x.com, polymarket status, etc).
* Consider flipping the scraper kill-switch at
  `/admin/scrapers` for the affected source so the rest of the
  scrapers keep working while we debug.

## Prevention

* `scraper_lag_alert` cron fires when `MAX(extracted_at)` is more
  than 90 minutes old.
* Per-scraper health panel at `/admin/scrapers` shows per-source
  last-run + error count.
* OOM guard: scrapers are ulimited via their systemd unit.

## Postmortem

Not required unless:
* Outage ran > 6 hours end-to-end.
* Users visibly affected (support tickets, Discord complaints).

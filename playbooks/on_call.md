# On-call

## Current coverage

**Single developer** (Julian). Best-effort, not 24/7.

The `/status` page and Terms both disclose this — we don't pretend
to cover midnight-to-6am European time. A SEV-1 paged at 3am
local gets an ack within the SLA (15 min) but the deeper
investigation likely waits until morning unless the site is
fully down.

## Pager sources

| Source | Where it fires | What it wakes up |
| --- | --- | --- |
| Uptime monitor | Slack `#incidents` + SMS | Any 5xx or timeout on `narve.ai/health` for > 3 min |
| Stripe webhook failure cron | Slack `#incidents` | > 10 webhooks failed in 15 min |
| Claude cost alert | Slack `#ops` | Daily spend > $50 |
| Auth fail spike | Slack `#ops` | > 20 failed logins/min |
| Scraper lag | Slack `#ops` | `MAX(extracted_at)` > 90 min old |
| Disk space | Slack `#ops` | `/` > 90% full |
| Backup cron | Slack `#ops` | Failed backup |

`#incidents` is the SEV-1/SEV-2 channel. `#ops` is SEV-3/SEV-4.
Only `#incidents` triggers SMS.

## When joining the on-call rotation

Second dev onboarding — weekly rotation, Mon 09:00 local to Mon
09:00 local. Handoff the Monday morning of your rotation:

1. **Read the last 7 days of `#incidents`.** Any open
   investigations carry over.
2. **Check `/admin/incidents`** for anything flagged "watching".
3. **Check the deploy log.** Anything shipped in the last 72
   hours that hasn't cleared its observation window is your
   problem during rotation.
4. **Acknowledge in `#on-call`.** Short message: "On-call from
   <date>. Watching <list>."

## Handoff doc template

At the end of your rotation, post to `#on-call`:

```
## <date> handoff

### Incidents this week
- <SEV-N>: <slug> — resolved / postmortem / in-flight

### Watching
- <thing>: <why we care>
- ...

### Scheduled maintenance ahead
- <date>: <maintenance>

### Known-flaky
- <alert>: <reason it's expected to flap>
```

Next on-call reads this before logging off Monday morning.

## Boundaries

**You are not required to** answer Slack outside your rotation
unless SEV-1. A civilised incident culture starts with that line
being real.

**You are required to** write the postmortem for any SEV-1 or
SEV-2 you touched, within 48 hours. Exception: if the next
on-call inherited the incident, they write it.

## Escalation

For incidents beyond our team's scope:

| Vendor | Contact |
| --- | --- |
| Stripe | Dashboard → Support → chat or phone |
| Cloudflare | Dashboard → Support (Enterprise plan — 24/7) |
| Anthropic | console.anthropic.com → Support |
| Polymarket | discord.gg/polymarket + email |
| Kalshi | support@kalshi.com |

Keep a local copy of the escalation matrix at
`~/Habbig/gateway/ESCALATION.md` (mirrored into this repo if
you edit it). Don't rely on being able to reach the vendor page
during an outage.

## Tools

* `ssh julianhabbig@100.69.44.108` — prod host.
* `tail -F /tmp/gateway.log` — request log.
* `/admin/health` — internal health dashboard.
* `/admin/performance` — latency + query stats.
* `/admin/security` — audit log + forensics.
* `playbooks/` — this directory.
* [`RUNBOOK.md`](../RUNBOOK.md) — deploy + rollback + backup.

## Promise to users

From `/status`:

> narve.ai is run by a small team. We respond to SEV-1 incidents
> (site down, data loss) within 15 minutes, 24/7, and fix them
> within 2 hours. Smaller incidents get best-effort attention
> during working hours (Europe/London).

Update this line as team size grows; it's a load-bearing
commitment in our Terms.

# SEV-2 — Stripe webhook flood

Symptoms: the in-process rate limiter's /stripe/webhook bucket trips,
or `tail -f /tmp/gateway.log | grep stripe_webhook` shows sustained
high request rate (> 10 req/s).

## First 5 minutes

**Distinguish replay vs attack.**

```bash
tail -500 /tmp/gateway.log | grep stripe_webhook | tail -100 | \
  grep -oE "evt_[A-Za-z0-9]+" | sort | uniq -c | sort -rn | head -20
```

* **One `evt_*` dominating the list** → legitimate Stripe retry.
  Stripe retries any non-2xx for up to 3 days; something is making
  our handler fail.
* **Many distinct `evt_*`, high volume** → possibly an attacker
  forwarding events, or a spam of webhook-lookalike requests.

## Replay case — fix the handler

```bash
grep -E "ERROR|Traceback" /tmp/gateway.log | grep -A 20 stripe | tail -60
```

Common root causes:

| Log line | Cause | Fix |
| --- | --- | --- |
| `FOREIGN KEY constraint failed` | webhook references a user row we no longer have | catch + mark_processed with error; Stripe stops retrying after 2xx |
| `KeyError: 'subscription'` | Stripe payload shape drift | update the per-event branch; ship + restart |
| `OperationalError: database is locked` | concurrent writer blocking | see `site_down.md` "database locked" |
| handler timeouts | slow downstream (Claude call inside a webhook) | move the slow work into an ARQ job |

Once fixed and deployed, Stripe's next retry lands a 2xx and the
flood stops.

## Attack case — verify, then rate-limit at Cloudflare

**Verify signatures are being checked.** Our handler rejects
unsigned webhooks, so attacker payloads never get into
`processed_stripe_events`.

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT COUNT(*) FROM processed_stripe_events \
   WHERE received_at > strftime('%s','now','-1 hour')"
```

Compare against Stripe dashboard's "Webhooks → narve endpoint →
Events" count for the same window. If ours is much higher, we're
accepting events without signatures somewhere — investigate
immediately (data integrity risk).

**Rate-limit at Cloudflare.** Cloudflare dashboard → Security →
WAF → Custom Rules. Add:

```
Field:    URI Path
Operator: equals
Value:    /stripe/webhook

Field:    IP source address
Operator: does not equal
Value:    <Stripe's IP range from stripe.com/docs/ips>

Action:   Block
```

Stripe publishes its egress IP range at `stripe.com/ips/webhooks.json`.
Update the rule with the current ranges (they change quarterly).

## Don't shut off the handler

Turning off the webhook endpoint breaks billing entirely — revenue
drops until we re-enable. Prefer: fix the cause, or rate-limit to
non-Stripe IPs at the edge.

## Post-flood cleanup

After the flood ends:

```bash
# Find the time window of the flood.
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT strftime('%Y-%m-%d %H:%M', datetime(received_at, 'unixepoch')) AS hr, \
          COUNT(*) AS cnt \
   FROM processed_stripe_events \
   WHERE received_at > strftime('%s','now','-6 hours') \
   GROUP BY hr ORDER BY hr"

# Spot-check for rows that errored.
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT event_id, event_type, error \
   FROM processed_stripe_events \
   WHERE error IS NOT NULL \
     AND received_at > strftime('%s','now','-6 hours') \
   LIMIT 20"
```

For each errored event: check Stripe dashboard; if our state
diverged from Stripe, run the `reconcile_subscriptions` job
on-demand:

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "INSERT INTO job_queue (job_name, enqueued_at, status) \
   VALUES ('reconcile_subscriptions', strftime('%s','now'), 'pending')"
```

## Postmortem

Fill in [`postmortem_template.md`](postmortem_template.md) if:
* Any customer state was left inconsistent (sub still active on
  Stripe but cancelled locally, or vice versa).
* The flood lasted > 10 minutes.
* We had to rate-limit at Cloudflare.

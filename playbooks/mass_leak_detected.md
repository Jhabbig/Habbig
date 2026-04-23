# SEV-2 — Platform data leaked publicly

Symptoms: a screenshot, text dump, or scraped archive of
narve-internal content shows up on Twitter/X, Discord, Reddit, etc.
Affiliate-detection alert may fire if the leaker also has an
affiliate account.

The forensic watermark + signing system is built for this exact
path; the playbook is the on-ramp.

## Step 1 — preserve evidence BEFORE investigating

The post can be deleted by the leaker the moment they notice
attention. Grab:

* The full image (`Save image as…` from the browser, not a
  screenshot of a screenshot — we need the pixel fidelity for
  watermark extraction).
* The source URL + post timestamp.
* The poster's public handle.
* Any visible context (replies, quote-tweets, Discord channel
  name + server).

Store under `~/leak-<YYYYMMDD-HHMM>-<slug>/` on the host where
you're running forensics. Copy everything that survives into
`~/Habbig/gateway/forensics/evidence/` eventually so the incident
trail is append-only.

## Step 2 — run the watermark extractor

Our canvas-steganography layer writes an invisible user-id to every
authenticated page. Extract it from the image:

```bash
ssh julianhabbig@100.69.44.108
cd ~/Habbig
python3 gateway/forensics/extract_watermark.py /tmp/leaked.png --top-n 5
```

Output: top-N candidate user_ids with confidence scores. Typical
expectations:

* **A single user_id at ≥ 0.9 confidence** → reliable attribution.
* **Multiple candidates at ≥ 0.6** → partial extraction; cross-
  reference with other signals before acting.
* **No hits at all** → image was heavily re-encoded (JPEG quality
  ≤ 50%) or the leaker cropped aggressively. Fall back to the text
  attribution path if there's visible copy.

## Step 3 — if the leak is text

```bash
python3 gateway/forensics/attribute_text_leak.py /tmp/leaked.txt
```

Uses the per-user signing fingerprint — invisible zero-width chars
embedded in displayed text that a copy-paste carries along. Output
is the same top-N-candidates format.

## Step 4 — cross-reference independent signals

**Never act on watermark alone.** Confirm with at least one
independent signal before escalating:

```bash
# Capture-attempt events for the candidate user in the hours before
# the post:
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT timestamp, event_type, details \
   FROM security_events \
   WHERE user_id = ? AND timestamp > ? AND timestamp < ? \
   ORDER BY timestamp DESC" \
  <candidate_user_id> <post_time - 3600> <post_time>

# Session + login history:
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT last_active_at, ip_address, user_agent \
   FROM user_sessions \
   WHERE user_id = ? ORDER BY last_active_at DESC LIMIT 5" \
  <candidate_user_id>

# Subscription status (paying customers are rarer offenders):
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT subscription_tier, subproduct_subscriptions \
   FROM users WHERE id = ?" \
  <candidate_user_id>
```

Two independent positives → act. One positive only → log the
incident, monitor, don't act.

## Step 5 — act

Once attribution is solid:

**Suspend the account.**

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "UPDATE users SET suspended = 1, suspended_reason = 'ToS violation: data exfiltration' \
   WHERE id = ?" \
  <user_id>
```

**Revoke all sessions.**

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "UPDATE user_sessions SET revoked = 1, revoked_reason = 'leak_suspension' \
   WHERE user_id = ?" \
  <user_id>
```

**If they have an affiliate account, freeze it.**

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "UPDATE affiliate_accounts SET status = 'frozen' WHERE user_id = ?" \
  <user_id>
```

**Send the evidence email.** Usually prompts admission + return of
future access in exchange for taking down the post:

```
Subject: Account suspended — ToS violation

We detected redistribution of narve.ai content at <post URL>
posted at <timestamp>. Our forensics attribute this to your
account (user_id <X>) via canvas watermark + <other signal>.

Per Terms §<n>, your account is suspended. Affiliate commissions
are frozen. Reply to this email within 72 hours to dispute.
```

Keep the full evidence packet — watermark extractor output,
security-events query, session log — until the dispute window
closes.

## Step 6 — log in the incident register

```bash
# Append to gateway/forensics/incidents.md
cat >> ~/Habbig/gateway/forensics/incidents.md <<EOF

## <YYYY-MM-DD> — <slug>

- Source URL: <>
- Post time: <UTC>
- Detected at: <UTC>
- Candidate user_id: <>
- Confidence: watermark <x.xx>, text-sign <x.xx>
- Independent signals: <list>
- Action: <suspension / warning / dismissed>
- Evidence path: ~/Habbig/gateway/forensics/evidence/<date>-<slug>/
EOF
```

## What NOT to do

* **Don't post publicly about the leak.** The Streisand effect
  amplifies the leak; attribution + quiet action is more
  effective.
* **Don't DM the leaker before you have attribution.** They'll
  delete evidence.
* **Don't share the forensic output with anyone outside the
  admin list.** The watermark algorithm's effectiveness depends
  on its specifics not being public.

## Postmortem

Fill [`postmortem_template.md`](postmortem_template.md) if the
leak included Pro-tier content (> $20/mo paywall) or exceeded
100 measured views. Otherwise, the `forensics/incidents.md`
entry is enough.

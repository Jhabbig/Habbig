# SEV-3 — Credential-stuffing / suspicious login pattern

Symptoms: the `auth_fail_rate` cron (every 5 min) alerts at > 20
failed-login attempts per minute, or the admin `/admin/security`
dashboard shows a cluster of failed logins on a single
account or from an unusual IP range.

## First 2 minutes

**Confirm the alert is real.**

```bash
ssh julianhabbig@100.69.44.108
sqlite3 ~/Habbig/gateway/auth.db "
  SELECT datetime(timestamp, 'unixepoch') AS ts,
         ip_address, COUNT(*) AS n
    FROM audit_log
   WHERE action = 'login_failed'
     AND timestamp > strftime('%s','now','-10 minutes')
   GROUP BY ip_address
   ORDER BY n DESC LIMIT 20
"
```

* **Single IP with > 50 attempts** → automated, most likely a
  credential-stuffing bot. See "Single-source" below.
* **Many IPs, each with < 20 attempts** → distributed / botnet-
  sourced. See "Distributed" below.
* **Many attempts against a single email** → targeted account
  takeover. See `admin_account_takeover.md` if it's an admin
  email.

## Single-source

**Block the IP at Cloudflare.**

1. Cloudflare dashboard → Security → WAF → IP Access Rules.
2. Add the IP, scope: website, action: Block. Note comment
   (date + "credential stuffing").

**Confirm the block.**

```bash
# No more rows should appear from that IP after the block.
watch -n 5 "sqlite3 ~/Habbig/gateway/auth.db \
  \"SELECT COUNT(*) FROM audit_log \
    WHERE ip_address = '<IP>' AND action = 'login_failed' \
    AND timestamp > strftime('%s','now','-5 minutes')\""
```

## Distributed

**Enable Cloudflare managed challenge on `/login` + `/auth/*`.**

1. Security → WAF → Custom Rules → Add rule:

```
When incoming requests match:
  Field:    URI Path
  Operator: starts with
  Value:    /auth/

Then:
  Action: Managed Challenge
```

Managed challenge is invisible to legitimate users with good
IP reputation and a blocking hurdle for botnets. Leave it on
for 24 hours, then re-evaluate.

**Monitor for successful logins from the affected pool.** If
any of the distributed attackers succeed, they've matched a
real password — prompt-reset those accounts immediately:

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "SELECT DISTINCT u.email \
   FROM audit_log a JOIN users u ON u.id = a.user_id \
   WHERE a.action = 'login_success' \
     AND a.timestamp > strftime('%s','now','-1 hour') \
     AND a.ip_address IN ( \
       SELECT DISTINCT ip_address FROM audit_log \
       WHERE action = 'login_failed' \
         AND timestamp > strftime('%s','now','-1 hour') \
     )"
```

For each returned email, enqueue a password-reset (which
invalidates current sessions):

```bash
# Via admin panel: /admin/users/<id> → "Force password reset"
```

## After the incident

**Short-term.** Watch the alert for a few hours; if it subsides,
revert the managed-challenge rule (otherwise regular users hit
more friction than necessary).

**Medium-term.** Check the IP/ASN against threat-intel
databases. If the pattern matches a known botnet, share the
list with CF via their "Report threat" feature so the managed
list picks it up globally.

**Long-term.** Our login flow has:
* Rate limiting (10 fails in 5 min per email, 30 per IP).
* Progressive backoff on failures.
* Generic error messages ("Invalid email or password") that
  don't reveal whether an email exists.
* CAPTCHA on the `/signup` path but NOT on `/login` (UX
  tradeoff).

If we keep getting stuffing attempts, consider adding CAPTCHA
to `/login` after N failures from the same IP.

## Postmortem

Not required for SEV-3 unless:
* > 5 accounts actually compromised.
* Forced large-scale password-reset wave.
* We had to make a visible UX change (CAPTCHA) that affects
  legitimate users.

# SEV-1 — Suspected admin account compromise

Symptoms: an admin action appeared in the audit log that the
real admin didn't perform. An admin email received a
"your password was changed" notification that the owner didn't
trigger. An admin's session resumed from an unusual
geo / ASN. Any of these is enough to assume compromise.

**Act first, investigate second.** Admin compromise allows
lateral moves (impersonation, DB dumps, affiliate payout
manipulation) that escalate fast; minutes matter.

## Step 1 — freeze (2 minutes)

**Revoke ALL sessions for every admin.**

```bash
ssh julianhabbig@100.69.44.108
sqlite3 ~/Habbig/gateway/auth.db "
  UPDATE user_sessions
     SET revoked = 1,
         revoked_reason = 'admin_security_incident'
   WHERE user_id IN (SELECT id FROM users WHERE role >= 1)
     AND revoked = 0
"
```

Every admin is logged out. They'll be annoyed; good.

**Rotate admin passwords.**

```bash
cd ~/Habbig
# Generate a strong random password per admin, capture the hash:
python3 -c "
import secrets
from gateway.queries.auth import hash_password
pw = secrets.token_urlsafe(32)
print('password:', pw)
print('hash:', hash_password(pw))
"
```

Update the DB directly (do NOT go through any public
password-reset flow — the attacker might intercept the reset
email):

```bash
sqlite3 ~/Habbig/gateway/auth.db \
  "UPDATE users SET password_hash = ? WHERE email = ?" \
  '<new_hash>' 'admin@narve.ai'
```

Distribute each new password over a secure channel — Signal,
in-person, a separate email account that hasn't been
compromised. NOT the narve.ai email the admin normally uses
(the attacker may control that).

Repeat per admin (`role >= 1`).

## Step 2 — audit the last 7 days (15 minutes)

**Admin actions.**

```bash
sqlite3 ~/Habbig/gateway/auth.db "
  SELECT datetime(timestamp, 'unixepoch') AS ts,
         admin_user_id, action, target_type, target_id,
         ip_address, user_agent
    FROM audit_log
   WHERE admin_user_id IS NOT NULL
     AND timestamp > strftime('%s','now','-7 days')
   ORDER BY timestamp DESC
"
```

Walk the list with the real admins. Any action they didn't
perform → note for reversal.

**Impersonation sessions.**

```bash
sqlite3 ~/Habbig/gateway/auth.db "
  SELECT datetime(started_at, 'unixepoch') AS started,
         admin_user_id, target_user_id, ended_at, reason
    FROM impersonation_sessions
   WHERE started_at > strftime('%s','now','-7 days')
   ORDER BY started_at DESC
"
```

Any impersonation the admin doesn't remember starting → log as
an incident event.

**Stripe / affiliate / billing mutations.** Particularly
dangerous — an attacker could set their own affiliate payout
email or gift themselves subscriptions.

```bash
sqlite3 ~/Habbig/gateway/auth.db "
  SELECT datetime(timestamp, 'unixepoch') AS ts,
         admin_user_id, action, target_type, target_id
    FROM audit_log
   WHERE timestamp > strftime('%s','now','-7 days')
     AND (action LIKE '%affiliate%'
       OR action LIKE '%gift%'
       OR action LIKE '%payout%'
       OR action LIKE '%billing%'
       OR action LIKE '%subscription%')
   ORDER BY timestamp DESC
"
```

## Step 3 — reverse damage

For each unauthorised action from Step 2:

* **User suspended maliciously** → flip `suspended` back to 0.
* **Gift subscription created** → delete the row; email the
  recipient explaining.
* **Affiliate payout redirected** → revert `payout_email`;
  freeze the affiliate account pending review.
* **Data exported** → note in the incident; cannot be un-
  exported, but the export evidence remains in audit_log.

## Step 4 — rotate secrets

Change every admin-scoped secret the attacker could have read:

* Stripe secret key → Stripe dashboard → Developers → API keys →
  roll. Update `~/.gateway_env` + restart.
* `GATEWAY_COOKIE_SECRET` → regenerate, update
  `~/.gateway_env`, restart. All non-admin sessions invalidate
  too; worth it.
* `CREDENTIALS_ENCRYPTION_KEY` → only rotate if you're
  confident the attacker didn't decrypt any stored credentials.
  Rotating requires re-encrypting every `user_market_credentials`
  row — expensive; do it if in doubt.
* Anthropic / Claude API key → console.anthropic.com → roll.
* Any 3rd-party keys in `~/.gateway_env`.

## Step 5 — confirm narrow blast radius

**All user sessions (not just admin) — review recent admin-
initiated session revocations.** If the attacker tried to
cover tracks by revoking sessions, the audit log catches it.

**Check data exports.** If any `data_export_requests` row was
created by the compromised admin in the last 7 days, assume the
exported file was exfiltrated. The export usually emails a
pre-signed URL — the attacker has the URL too.

## Step 6 — external disclosure

Required when:
* User data was demonstrably accessed by the attacker
  (impersonation used, export downloaded, affiliate payout
  redirected).

Template email to affected users:

```
Subject: Security incident — your narve.ai account

On <date>, we detected unauthorised access to an
administrative account. We immediately revoked sessions and
rotated credentials.

Your data may have been viewed. We have no evidence it was
modified; specifically, <list affected datasets>.

We've logged you out of all sessions as a precaution. Your
password is unchanged, but we recommend rotating it at
narve.ai/settings/security.

Incident report: <URL>. Questions: security@narve.ai.
```

Follow with a public postmortem within 7 days.

## Postmortem

Mandatory. See [`postmortem_template.md`](postmortem_template.md).
Include:
* Root cause (phishing, password reuse, token theft, etc.).
* Scope of access (what they could and did see).
* Dwell time (first unauthorised action → last).
* What we've changed to prevent recurrence (2FA enforcement,
  IP allowlisting on the admin panel, etc.).

## Prevention checklist

* 2FA on admin accounts (required, not optional). See
  migration 019 (reserved slot for 2FA re-add).
* IP allowlist for `/admin/*` at Cloudflare (home + office IPs).
* Slack alert on every admin action — surfaces unauthorised
  activity in real-time, not days later.
* Admin sessions expire in 24 h instead of 90 d.
* Every admin action goes through `audit_log` — already done,
  don't regress.

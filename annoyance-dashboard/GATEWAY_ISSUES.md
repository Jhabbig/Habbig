# Gateway issues raised by annoyance-dashboard

Issues surfaced while building the annoyance dashboard that need to be filed
against `~/Habbig/gateway/`. Not fixable here because the code lives in the
gateway repo and is shared with the other dashboards.

File each of these as a gateway issue when the GitHub MCP is back online
(gh CLI or the GitHub MCP — both disconnected at the time this file was
written).

---

## [P8.3] Unsubscribe link in outbound emails is unsigned

**Severity:** LOW
**Source:** Annoyance dashboard security audit, P8 sweep (2026-04-20).
**Affects:** gateway `/profile#email-preferences` handler and every outbound
email template across every dashboard that uses the shared email system.

### What happens today

`notifications.py::UNSUBSCRIBE_URL` defaults to
`https://narve.ai/profile#email-preferences` and is passed verbatim into
`email_templates/spike_alert.html`. The gateway route at that URL reads the
current session cookie and shows the logged-in user's email prefs.

Two problems follow:

1. **No one-click unsubscribe.** An email recipient who forwards the email
   to someone else, or who isn't currently logged in, can't unsubscribe from
   the link directly — they get redirected to login. Recipients who just
   want out without logging in have no way to act.
2. **No attribution in the link.** There's no token in the URL that the
   gateway could use to identify "this email to this user for this dashboard".
   So even when a logged-in user does click it, the gateway can't pre-select
   "annoyance" as the specific channel to unsubscribe from — they have to
   find it themselves in the prefs UI.

### What we want

The gateway should expose a signed unsubscribe URL of the form:

    https://narve.ai/unsubscribe?u=<user_id>&c=<channel>&t=<hmac>

Where:

- `c` is a channel identifier (e.g. `annoyance-spike-alerts`, future values
  like `happiness-digest` etc.)
- `t` is an HMAC of `(user_id, channel, server_secret)`, truncated to ~22
  bytes (url-safe base64). No expiry necessary — unsubscribe is idempotent;
  the token just proves the sender (us) addressed the email to this
  specific user/channel pair.
- The handler validates the HMAC, flips the relevant `users.*_subscribed`
  flag to 0, shows a "You're unsubscribed" page, and offers a "re-subscribe"
  button.

### What we need from the gateway side

- New route `GET /unsubscribe` that accepts `u`, `c`, `t`.
- HMAC helper in `gateway/email_system/` that both the gateway and each
  dashboard can import to MINT the token when sending, and the gateway uses
  to VERIFY on click.
- Schema: a per-channel subscribe flag (e.g. `users.annoyance_alerts_subscribed`
  NOT NULL DEFAULT 1). We need something distinct from the existing
  `email_marketing` boolean because annoyance alerts are a paid-tier service
  comm, not marketing.
- Migration adding the column(s) + a default of 1 for all existing Pro users.

### Consumer changes on the annoyance-dashboard side (do after gateway ships)

In `notifications.py::send_spike_email`, construct the unsubscribe URL
per-recipient using the new HMAC helper, rather than using the shared
`UNSUBSCRIBE_URL` env default. Pass that to `_render_template`. Drop the
module-level `UNSUBSCRIBE_URL` constant.

### Suggested file

- `~/Habbig/gateway/server.py` — new `/unsubscribe` route
- `~/Habbig/gateway/email_system/unsubscribe.py` — new `mint_token()` /
  `verify_token()` helpers
- `~/Habbig/gateway/migrations/0XXX_email_channel_subscriptions.py` —
  per-channel subscribe booleans

### References

- `notifications.py:63-66` — where `UNSUBSCRIBE_URL` is currently defined
- `email_templates/spike_alert.html` — where the URL is interpolated
- `gateway/security/csrf.py` — the HMAC token pattern we should mirror
  (it does basically the same thing for CSRF)

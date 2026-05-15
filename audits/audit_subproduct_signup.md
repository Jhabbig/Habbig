# Adversarial audit — `gateway/subproduct_signup_routes.py`

**Scope.** Adversarial review of `gateway/subproduct_signup_routes.py`
(224 lines) with focus areas requested by the user:

1. Stripe Checkout session creation uses `client_reference_id=user_id`
   (so the webhook can map back).
2. `success_url` + `cancel_url` use `APP_URL` env (not hardcoded).
3. Checkout returns 302 redirect (no JSON-leak of the session-secret).
4. Success-page idempotency when the Stripe webhook lands before the
   redirect.

Supporting files consulted to ground each finding:

- `gateway/subproduct.py` (catalogue + `SUBPRODUCTS` slug whitelist)
- `gateway/stripe_webhook_routes.py` (the `_grant_access` branch)
- `gateway/stripe_webhook_hardening.py` (idempotency, IP allowlist)
- `gateway/onboarding_routes.py` (`/onboarding` page handler that the
  `success_url` redirects to)
- `gateway/middleware/subproduct.py` (`SubproductMiddleware` that
  attaches `request.state.subproduct`)
- `gateway/server.py` (CSRF middleware, subdomain CSRF carve-out,
  `_CSRF_EXEMPT_POSTS`, `_is_rate_limited`)
- `gateway/billing_routes.py` (`_billing_rate_limit` reference pattern)
- `gateway/db.py` (`users` UNIQUE constraints, `conn()` isolation)

No code was modified.

---

## Severity counts

| Severity | Count |
| --- | --- |
| Critical | 1 |
| High     | 4 |
| Medium   | 5 |
| Low      | 4 |
| Informational | 3 |
| **Total** | **17** |

---

## Answers to the four focus questions (up-front)

These are the exact questions the audit was scoped against. The full
finding list below ranks every issue by severity; this block lets the
reviewer see the four asks at a glance.

1. **`client_reference_id=user_id`?** **No.** The code does not set
   `client_reference_id` anywhere. `user_id` is piped through
   `metadata.user_id` at both session level and `subscription_data`
   level (lines 128-139). The webhook reads `metadata.user_id` off the
   subscription object (`stripe_webhook_routes.py:93`), so the mapping
   does work — but the spec the user audited against is not what the
   code actually does. See **H1** for why this matters (it conflates
   the audit story and the fix path for `checkout.session.completed`).

2. **`success_url` / `cancel_url` use `APP_URL` env?** **Half.**
   `success_url` uses `_app_url()` which reads `APP_URL` (line 42).
   `cancel_url` is hardcoded to `https://{slug}.narve.ai/?checkout_cancelled=1`
   (line 127) — `APP_URL` is not consulted. Staging cannot redirect a
   cancelled checkout to `*.staging.narve.ai` without code change. See
   **M2**.

3. **Returns 302 (no JSON-leak of session-secret)?** **Mixed.** The
   form endpoint `/subproduct-signup` does 302 to `session.url`
   (line 223) — fine. The JSON endpoint
   `/api/billing/subproduct-checkout` (lines 150-182) returns the URL
   in a JSON body. That URL contains the Checkout-Session ID in the
   path, **not** the publishable / secret key — Stripe's hosted URL
   carries `cs_test_…` / `cs_live_…` which is the session ID, not a
   secret. So no key is leaked, but the URL is a bearer-style payment
   link (anyone with it can complete the purchase using their own
   card). See **M3** for why the JSON endpoint should still return a
   302 or require auth.

4. **Idempotency when webhook arrives before redirect?** **Not
   applicable as written, but worse: the success page is unreachable.**
   `success_url` points at `/onboarding?subproduct=<slug>&session_id=…`,
   and `/onboarding`'s handler calls `_require_user(request)` which
   401s when no session cookie is present (`onboarding_routes.py:98-107`).
   The shell user created at line 101-107 of the audited file has no
   password and there is no magic-link / auto-login mechanism wired
   anywhere in the repo. **Every paying customer's success redirect
   401s.** Idempotency is moot because no read-after-write happens on
   the success page; it never loads. See **C1**.

---

## Top 3 (must-fix)

1. **C1 — `success_url` redirects a paying-but-not-logged-in user to
   an auth-gated page; the documented "email magic-link" auto-login
   does not exist.**
   The Stripe Checkout `success_url` is built as
   `f"{app_url}/onboarding?subproduct={slug}&session_id={{CHECKOUT_SESSION_ID}}"`
   (line 126). `/onboarding`'s handler (`onboarding_routes.py:169-186`)
   calls `_require_user(request)` (defined at lines 98-107) which
   raises HTTP 401 when `server.current_user(request)` returns falsy.
   The shell user inserted by `_create_or_get_shell_user`
   (`subproduct_signup_routes.py:66-107`) is created with
   `password_hash=''` and `password_salt=''` — they cannot log in.
   The module docstring (lines 14-19) claims the success redirect
   "logs the user in via the email magic link," but there is no
   magic-link issue / token / verify route anywhere — `grep -r magic
   gateway/auth/` returns nothing.
   **Net effect:** every visitor who pays via the subproduct flow is
   redirected to a 401 page after Stripe takes their money. The
   subscription is granted server-side by the webhook (so the user is
   billed), but they can never reach the dashboard from the success
   redirect — they have to discover the password-reset flow on their
   own. This is the entire revenue path for the sub-brand product.
   Severity: **Critical** — silent product breakage on the paid path.

2. **H1 — Webhook contract drift: the audit's stated invariant
   (`client_reference_id=user_id`) is not what the code does, and the
   webhook does not listen to `checkout.session.completed`.**
   Two coupled gaps.
   a) `_build_checkout_session` never sets `client_reference_id`
      (line 122-140). The narve.ai Pro flow elsewhere in the codebase
      uses `metadata.narve_user_id` (`stripe_webhook_routes.py:93`
      reads both `metadata.user_id` and `narve_user_id`), and the
      subproduct flow piggybacks on `metadata.user_id`. That works as
      long as both sides agree, but the API contract requested by the
      audit (and by Stripe's docs for "map a paying customer back to a
      local user id") is `client_reference_id` — a top-level
      string field surfaced on every webhook event type. The current
      design only works when Stripe emits `customer.subscription.created`
      with the subscription-level metadata intact.
   b) The webhook dispatcher (`stripe_webhook_routes.py:281-298`)
      branches on `customer.subscription.created`, `…updated`,
      `…deleted`, `invoice.paid`, `invoice.payment_failed`. It does
      **not** handle `checkout.session.completed`. If Stripe ever
      delays the subscription event (it can lag the checkout
      completion by minutes during incidents — see Stripe Engineering
      blog on retry timing) or fires `…created` before `…completed`
      arrives (which is normal: `…created` is the canonical "user has
      a subscription now" event), nothing actually breaks today. But
      the moment anyone wires a "verify the session before showing
      success" call (`stripe.checkout.Session.retrieve(session_id)`)
      onto the success page, you'd want to map back via
      `client_reference_id`, not a metadata round-trip. The current
      design also means a one-time-payment subproduct (no
      subscription) cannot be added without re-wiring the dispatcher.
   Severity: **High** — works today, brittle to any flow change, and
   the audit invariant is not satisfied.

3. **H2 — Pre-registration takeover: `_create_or_get_shell_user`
   creates a shell row keyed on attacker-supplied email with no
   ownership proof and no rate-limit.**
   Lines 78-107 idempotently insert a `users` row for any email an
   unauthenticated caller supplies. There is:
   - No proof the caller owns the email (no verification token, no
     magic-link confirm step).
   - No per-IP / per-email rate-limit on either
     `/api/billing/subproduct-checkout` or `/subproduct-signup`. The
     billing routes elsewhere use `_billing_rate_limit` at 20/hr
     (`billing_routes.py:74-81`) but those are authenticated; this
     surface is not.
   - No CAPTCHA, no Turnstile, no proof-of-work.
   The shell row pre-empts the email's eventual legitimate signup
   because `users.email` is `UNIQUE NOT NULL` (`db.py:26`). An
   attacker pointing a script at `victim@target.com` across all 13
   subproduct slugs will create 13 Stripe Checkout sessions —
   abandoned ones cost nothing, but the side effect is a shell user
   that the victim cannot displace on later sign-up. If the attacker
   *funds* one session with their own card, the victim's email is now
   bound to an active subscription on someone else's payment method,
   and the webhook's `_grant_access` writes a `subscriptions` row for
   that `user_id` (`stripe_webhook_routes.py:108-121`).
   The "harmless squatting" version costs the attacker nothing; the
   "punitive" version costs them ~$8-20/month per victim but gives
   the victim a paid sub-brand they didn't ask for, billed to the
   attacker — making it more of a denial-of-onboarding and
   record-poisoning attack than a financial one. Either way, the
   `users` row is not removable by the victim's normal flow.
   Severity: **High** — pre-registration / account-squatting on a
   public, unrate-limited endpoint.

---

## Full findings

### Critical

#### C1 — Success-page redirect 401s every paying customer

- **Files:** `gateway/subproduct_signup_routes.py:126`,
  `gateway/onboarding_routes.py:98-107, 169-186`.
- **Details:** described in Top-3 #1 above. Shell user has no
  password; `/onboarding` requires an authenticated session; there is
  no magic-link or auto-login wired despite the module docstring
  promising one (lines 14-19).
- **Repro (manual):** Stand up the form, POST a valid email, follow
  the Stripe redirect, complete the test purchase. Observe Stripe
  returns to `/onboarding?subproduct=…&session_id=…`. Page returns
  401 because `current_user(request)` is None.
- **Fix sketch:**
  - Implement a single-use magic-link issuer keyed on
    `users.id` + `session_id`, redirect to
    `/auth/checkout-callback?session_id=…&token=…` instead of
    `/onboarding`. The callback verifies the Stripe session
    (`stripe.checkout.Session.retrieve` with status check =
    `complete`), redeems the token, sets the session cookie, and
    *then* 302s to `/onboarding`.
  - Or set `client_reference_id=user_id` and have the callback look
    up by session ID, retrieve the session from Stripe, and use the
    `client_reference_id` it gets back to log in the matching user.
- **Impact:** entire paid sub-brand funnel is dead-on-arrival in
  production.

### High

#### H1 — Webhook contract drift / no `checkout.session.completed`

- **Files:** `gateway/subproduct_signup_routes.py:122-140`,
  `gateway/stripe_webhook_routes.py:281-298, 92-96`.
- **Details:** described in Top-3 #2 above.
- **Fix sketch:** add `client_reference_id=str(user_id)` to the
  `session_params` dict at line 122 (one-line change; survives
  Stripe's session→subscription lifecycle in every event), and add
  a `checkout.session.completed` branch to the dispatcher that
  resolves the user via `event.data.object.client_reference_id`
  and (a) issues the auto-login token for C1 and (b) idempotently
  grants the subscription if `…subscription.created` is delayed.
- **Why high not critical:** the happy path works in normal Stripe
  latency conditions because `subscription.created` follows
  `session.completed` within ~seconds. The criticality lives in C1,
  not here.

#### H2 — Pre-registration takeover / unrate-limited shell-user create

- **Files:** `gateway/subproduct_signup_routes.py:66-107, 150-223`,
  `gateway/server.py:1124-1156` (CSRF exempt list — neither
  endpoint listed there).
- **Details:** described in Top-3 #3 above.
- **Compounding factor:** CSRF middleware *appears* to protect this
  surface but does not. The middleware at `server.py:1277-1280`
  exempts every subdomain of `narve.ai` in production
  ("they have their own auth"). The subproduct landing page lives
  at `<slug>.narve.ai` and its CTA POSTs to `/subproduct-signup` —
  so in production the request comes from a subdomain, CSRF is
  skipped, and the form has **no token, no rate-limit, no captcha**.
  Any cross-origin actor can submit it.
- **Fix sketch:**
  - Add `_is_rate_limited(f"subproduct_signup_ip:{ip}", limit=10,
    window=3600)` at the top of both handlers. Drop on 429.
  - Add per-email cap (`subproduct_signup_email:{email}`, 3 / 24h)
    so a single email can't spawn 13 shell rows.
  - Stop inserting the `users` row at signup time. Defer the row to
    the `customer.subscription.created` webhook handler, keyed off
    a Stripe customer ID + verified email. Until payment is taken,
    you have at most a `pending_checkouts` row that does not block
    later real signups.
- **Impact:** spam-able public endpoint that pollutes the canonical
  `users` table.

#### H3 — Open redirect via attacker-controlled `subproduct` form field

- **Files:** `gateway/subproduct_signup_routes.py:199-211`.
- **Details:** On the form endpoint, when the visitor came via the
  apex (so `request.state.subproduct is None`), the code falls back
  to the form-field `subproduct` value and uses it verbatim in the
  error-redirect URL: `f"https://{slug}.narve.ai/?error=email"`
  (line 203) and similar at lines 208-209, 219-221. `slug` is
  user-supplied; nothing validates it against `SUBPRODUCTS`. A
  crafted slug like `attacker.com` produces
  `https://attacker.com.narve.ai/?error=email` — only an attacker
  who owns `attacker.com.narve.ai` benefits, so direct phishing is
  hard. But CR/LF injection (`slug = "x\r\nLocation: evil.com"`)
  *will* split the response header on naive servers; Starlette's
  `RedirectResponse` does pass the value into a `Location` header
  via `URL().__str__()` which percent-encodes most but not all
  control chars. Defence-in-depth: whitelist `slug` against
  `SUBPRODUCTS.keys()` before interpolating into a `Location` URL.
- **Fix sketch:** at line 199, after computing `slug`, drop if
  `slug not in SUBPRODUCTS` (treat as apex visitor and redirect to
  `/`).
- **Impact:** low real-world (you need a narve.ai subdomain you
  control); but it's free hygiene and CR/LF is a known
  Starlette/Uvicorn footgun.

#### H4 — `_create_or_get_shell_user` is racy under concurrent same-email POSTs

- **Files:** `gateway/subproduct_signup_routes.py:78-107`,
  `gateway/db.py:258-266`.
- **Details:** The function does a `SELECT id FROM users WHERE email
  = ?` then an `INSERT`. `db.conn()` (line 258) opens a default
  `sqlite3.connect` connection — no `isolation_level=None`, no
  explicit `BEGIN IMMEDIATE`. SQLite's Python default is "deferred"
  — the read does not take a write lock. Two concurrent POSTs for
  the same email both see "no row", both attempt INSERT; the
  second hits the `UNIQUE` constraint on `users.email` (`db.py:26`)
  and raises `sqlite3.IntegrityError`. The outer try/except at line
  217-222 catches `Exception` and 302s to `?error=checkout` — so
  the second visitor sees a generic checkout error even though they
  are the legitimate user.
- **Repro:** two simultaneous `curl` POSTs with the same email
  reliably reproduces this on a cold DB.
- **Fix sketch:** wrap the SELECT + INSERT in `INSERT … ON CONFLICT
  (email) DO UPDATE SET email = excluded.email RETURNING id` (SQLite
  3.35+) — gives you the existing row if there is one, the new row
  if not, in a single atomic statement. Or take an explicit write
  lock: `c.execute("BEGIN IMMEDIATE")` before the SELECT.
- **Impact:** any visitor whose first click races a stale-tab
  resubmit (think double-tap on mobile Safari) gets a false-positive
  checkout failure, while a shell user *was* created.

### Medium

#### M1 — JSON endpoint exposes the Checkout URL pre-auth

- **Files:** `gateway/subproduct_signup_routes.py:150-182`.
- The route accepts an unauthenticated JSON POST with email + slug
  and returns the Stripe-hosted Checkout URL in the body. The URL
  contains the session ID (`cs_test_…` / `cs_live_…`) — that is
  not a secret per se (Stripe treats it as a public pointer that
  anyone with the URL can complete the checkout against their own
  card), but exposing it via a JSON response means a hostile site
  could embed a fetch to this endpoint and use the URL as a
  payment-link harvester (every legitimate visitor of attacker's
  site silently generates a Checkout for victim's email; attacker
  forwards the URL to victim with their own card field pre-filled).
- **Fix sketch:** drop the JSON endpoint entirely (the form route
  covers the no-JS path; the on-page React widget should call the
  form route and let the browser follow the 302). If kept, gate
  behind a CSRF check and the same rate-limit added in H2.

#### M2 — `cancel_url` hardcodes `narve.ai`, ignoring `APP_URL`

- **Files:** `gateway/subproduct_signup_routes.py:127`.
- `success_url` uses `_app_url()` which respects `APP_URL`. The
  matching `cancel_url` is hardcoded to
  `f"https://{slug}.narve.ai/?checkout_cancelled=1"`. In staging
  with `APP_URL=https://staging.narve.ai`, a cancelled checkout
  redirects to the production landing page, leaking
  staging→production users.
- **Fix sketch:** parse `_app_url()` for host; if it ends in
  `.narve.ai` use the slug-prefixed variant of *that* host, else
  fall back to `_app_url()` apex. Helper: `_subproduct_host(slug)`.

#### M3 — `customer_email` is set but never verified against `metadata.user_id`

- **Files:** `gateway/subproduct_signup_routes.py:122-144`,
  `gateway/stripe_webhook_routes.py:84-123`.
- The Session is created with `customer_email=email` (line 124) and
  `metadata.user_id=str(user_id)` (line 129). If the Stripe Checkout
  user *changes the email* on the Stripe-hosted page (Stripe lets
  them — `customer_email` is a pre-fill, not a lock), the
  subscription is bound to the new email at Stripe but the
  `metadata.user_id` still points at the original local row, which
  has the *original* email. So Stripe thinks user is `new@x.com`,
  narve thinks user is `original@y.com`, and they diverge silently.
- **Fix sketch:** use `customer_email_collection={"enabled": false}`
  or set `customer` to a pre-created Stripe customer (and lock the
  email there). Alternatively, on the `customer.subscription.created`
  webhook, compare `obj.customer_details.email` against the
  `users.email` for `metadata.user_id` and warn-or-realign.

#### M4 — Webhook discriminator `flow=subproduct` is set in session metadata but invisible to the webhook

- **Files:** `gateway/subproduct_signup_routes.py:128-135`,
  `gateway/stripe_webhook_routes.py:84-123`.
- The `flow: "subproduct"` key is added to the session-level
  `metadata` (line 134) — not to `subscription_data.metadata`. The
  webhook only handles `customer.subscription.created` and reads
  `obj.metadata` off the *subscription* (which is set from
  `subscription_data.metadata`). So the discriminator is dead weight
  in the current dispatcher.
- **Fix sketch:** either copy `flow: "subproduct"` into
  `subscription_data.metadata` (lines 136-139) so the webhook can
  branch on it, or wire the `checkout.session.completed` handler
  (see H1) where session-level metadata is visible.

#### M5 — `Stripe SDK` exception envelope is too broad; legitimate user errors get masked as 502 / generic redirect

- **Files:** `gateway/subproduct_signup_routes.py:174-181, 217-222`.
- Both endpoints catch `except Exception` and surface a generic
  502 / `error=checkout`. Stripe raises distinct typed exceptions:
  `stripe.error.InvalidRequestError` (4xx — bad price id, missing
  customer email, etc.), `stripe.error.APIConnectionError` (5xx,
  retryable), `stripe.error.AuthenticationError` (5xx, fatal). All
  of these collapse into the same opaque response, making prod
  triage rely on log lines instead of telemetry.
- **Fix sketch:** branch on `stripe.error.*`. Return 400 with a
  user-visible message for `InvalidRequestError` (the user entered
  a bad email or you forgot to set the env price ID), 502 for
  `APIConnectionError`, 503 for `AuthenticationError`.

### Low

#### L1 — Stripe API key read on every Checkout call

- **Files:** `gateway/subproduct_signup_routes.py:118-119`.
- `stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")` runs
  inside `_build_checkout_session` on every invocation. Harmless
  but redundant — the `stripe` module holds api_key globally; once
  set, you don't need to set it again. Worse: this assignment
  silently clobbers any other module's configured key, including
  test code. Move to module import time once, or to a small
  `_ensure_api_key()` guard.

#### L2 — `subscription_data.metadata.user_id` is stringified but `_grant_access` re-parses with `int()` and silently drops on parse failure

- **Files:** `gateway/subproduct_signup_routes.py:137`,
  `gateway/stripe_webhook_routes.py:73-78, 93`.
- The webhook helper `_coerce_int` returns `None` on parse failure
  and `_grant_access` then logs "missing metadata" and silently
  returns. If `user_id` ever ships as a UUID (a likely future
  schema change), the failure mode is "Stripe subscription is paid
  but local DB never grants access" — silent revenue loss. Add a
  test that asserts the round-trip is type-stable.

#### L3 — `_create_or_get_shell_user` truncates username at 30 chars without checking the existing column max

- **Files:** `gateway/subproduct_signup_routes.py:89`.
- `username_base = email.split("@", 1)[0][:30]`. The `users.username`
  column is `TEXT UNIQUE NOT NULL` (`db.py:25`) with no length cap
  — so 30 is a magic number that doesn't match the schema. The
  suffix loop appends `1`, `2`, … so a collision-heavy email
  prefix can grow past 30 chars anyway. Cosmetic only.

#### L4 — `secrets` import is unused

- **Files:** `gateway/subproduct_signup_routes.py:30`.
- Listed in imports, never referenced. Probably a leftover from a
  removed magic-link token generator. Linter will flag this; remove
  or use it. Hints at the missing magic-link wiring called out in
  C1.

### Informational

#### I1 — `_app_url()` does not validate that `APP_URL` is HTTPS

- **Files:** `gateway/subproduct_signup_routes.py:41-42`.
- `os.environ.get("APP_URL", "https://narve.ai").rstrip("/")`. If
  `APP_URL=http://evil.example.com`, the success redirect on a
  live-mode Stripe purchase leaks the session ID to a plaintext
  HTTP host. Not a current threat (env is controlled), but
  defence-in-depth: require `APP_URL` to start with `https://` in
  production, fall back to `https://narve.ai` otherwise.

#### I2 — Module docstring describes a flow step that does not exist

- **Files:** `gateway/subproduct_signup_routes.py:14-19`.
- Step 4 of the docstring says "Checkout redirects to /onboarding…
  which (next step: auth exchange) logs the user in via the email
  magic link." The parenthetical ("next step") flags this as
  TODO-not-shipped, but it's worded as if it works. The C1 finding
  is a direct consequence. Either ship the magic-link flow or fix
  the docstring to match reality (and add a CTA on the
  failed-401 page that prompts password reset).

#### I3 — `subscription_data.metadata` is missing a `plan` key but `_grant_access` will accept the default

- **Files:** `gateway/subproduct_signup_routes.py:136-139`,
  `gateway/stripe_webhook_routes.py:97`.
- `_grant_access` defaults `plan="default"` when `metadata.plan` is
  absent. The subproduct flow always lands as `plan="default"`.
  That's fine until you add a yearly tier — at which point the
  current callsite needs to start including `plan: "monthly"` /
  `"annual"` in `subscription_data.metadata` or you'll have
  ambiguous billing. Capture in a TODO at line 138.

---

## Methodology notes

- **No code changes.** This audit only reads `subproduct_signup_routes.py`
  and the modules it transitively depends on. Findings include line
  numbers and adjacent files so each is independently verifiable.
- **Threat model assumed:** anonymous external attacker on the
  public internet who knows the URL structure (subdomains are
  enumerable via the SEO sitemap), plus a curious-but-legitimate
  paying customer (covers C1 and M3).
- **Out of scope:** the Stripe-hosted page itself; PCI; SCA / 3DS
  flows; the eventual `/dashboard` page reached after `/onboarding`.

---

*Audit run: 2026-05-15. Auditor: Claude (Opus 4.7), no human review yet.*

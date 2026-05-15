# Email Template Audit — `gateway/email_system/templates/`

**Date:** 2026-05-15
**Scope:** 34 `*.html` files under `gateway/email_system/templates/`.
**Methodology:** Read every template; cross-reference renderer (`renderer.py`) to determine escape semantics; grep for inline JS, external image URLs, `mailto:` patterns, and unsubscribe coverage; review sender call sites (`jobs/*.py`, `public_routes.py`, `status_routes.py`, etc.) for transactional vs promotional classification.

## Renderer semantics (load-bearing context)

`gateway/email_system/renderer.py` (`_render_vars`) escapes every `{{ var }}` with `html.escape()` **unless** the variable name starts with `raw_`, in which case it is emitted verbatim. There is no `|safe` filter — the prefix is the only opt-out. The base.html footer is wrapped around any template that contains a `{% block content %}` (31 of 34 templates). The three standalone templates (`base.html`, `morning_briefing.html`, `market_mover_alert.html`) ship their own `<html>` skeletons and bypass the base footer.

Severity legend used below:
- **HIGH** — exploitable today or compliance-breaking in production conditions.
- **MED** — defence-in-depth gap, latent risk if surrounding code changes, or unclear compliance posture.
- **LOW** — minor hygiene / informational.

---

## Severity counts

| Severity | Count |
|----------|------:|
| HIGH     | 2 |
| MED      | 6 |
| LOW      | 9 |
| **Findings (templates with at least one issue)** | **15 / 34** |

## Top 3 issues

1. **HIGH — `newsletter_blast.html`: `{{ raw_body_html }}` injects unescaped admin-supplied HTML.** Any operator with newsletter-publish access can ship arbitrary HTML to every subscriber. If that surface is ever delegated, or if the admin account is compromised, this is a fan-out XSS / phishing template. The renderer treats the `raw_` prefix as an explicit opt-out from escaping, so this is intentional, but it is the single largest blast radius in the email system.
2. **HIGH — Promotional / digest emails depend on the *caller* passing `unsubscribe_url`; templates fall back silently to "no unsubscribe link" if the key is missing.** `base.html` guards the footer link with `{% if unsubscribe_url %}`, and `morning_briefing.html` / `market_mover_alert.html` simply emit `<a href="{{ unsubscribe_url }}">` with no guard (an empty `href=""` if missing). There is no template-side enforcement that promotional/CAN-SPAM-covered classes always inject a link — a future caller change can drop the link and the template will not complain.
3. **MED — Two templates (`morning_briefing.html`, `market_mover_alert.html`) load `<img src="{{ app_url }}/_gateway_static/img/logo.png">` from the narve.ai gateway.** With a per-recipient `app_url` and no cache headers, this is functionally a tracking pixel (server-side request log captures recipient open + IP). Under GDPR/ePrivacy this needs to be disclosed in the privacy policy or moved to an inlined / data-URI / blocked-image fallback. Same templates also bypass `base.html`, so they inherit none of the standard footer protections.

---

## Per-template findings

### `base.html` — shared layout
- Variables: `{{ content }}` (replaced verbatim via `base.replace("{{ content }}", rendered_content)`), `{{ unsubscribe_url }}` (escaped), `{{ watermark }}` (escaped).
- `{{ content }}` is intentionally raw — it is the already-rendered child block. **OK.**
- External links: `https://narve.ai/privacy`, `https://narve.ai/terms` (hardcoded, first-party). **OK.**
- Unsubscribe: `{% if unsubscribe_url %}` — only renders when caller passes it. See HIGH #2.
- Inline JS / `mailto:` / external images: none.

### `welcome.html` — onboarding (transactional)
- All variables escaped: `display_name`, `tier`, `subproduct_name`, `subproduct_tagline`, `app_url`, `subproduct_url`. **OK.**
- Three mutually-exclusive branches: `is_pro_welcome` / `subproduct_name` / `is_generic_welcome`.
- **LOW:** subject line "Welcome, {{ display_name }}." — if `display_name` contains `<` characters they will be HTML-escaped, but they will be visible in the rendered subject as `&lt;`. Cosmetic.
- Unsubscribe: transactional, none required.

### `2fa_locked.html` — security notice (transactional)
- Variables escaped: `display_name`, `when`, `ip_address`. **OK.**
- External link: `https://narve.ai/forgot-password` hardcoded. **OK.**
- Unsubscribe: transactional/security, exempt.

### `2fa_email_otp.html` — OTP delivery (transactional)
- Variables escaped: `display_name`, `code`, `expires_in`. **OK.**
- The OTP is rendered inside a `<div>`. **OK.**
- Unsubscribe: transactional, exempt.

### `token_delivery.html` — invite token delivery (transactional)
- **MED:** `{{ raw_token }}` is rendered **unescaped**. The `raw_` prefix is the renderer's explicit opt-out. Today `secrets.token_urlsafe(24)` (see `unsubscribe.py:42`) only emits `A-Za-z0-9_-`, so escaping is a no-op. If a future change introduces HTML-bearing chars (e.g. switching to a different token format, custom prefix), this becomes XSS-on-mail-client.
- Other variables escaped: `display_name`, `app_url`. **OK.**
- Unsubscribe: transactional, exempt.

### `data_export_ready.html` — GDPR export delivery (transactional)
- Variables escaped: `display_name`, `file_size_kb`, `download_url`, `expires_at_iso`. **OK.**
- `{{ download_url }}` rendered both as `href` and as visible text — both branches escaped. **OK.**
- Unsubscribe: GDPR-mandated communication, exempt.

### `admin_security_alert.html` — internal alert (admin-only)
- Variables escaped: `user_id`, `user_email`, `count`. **OK.**
- External links: `https://narve.ai/admin/security/bulk-fetches`, `.../forensics` hardcoded. **OK.**
- **LOW:** `{{ user_email }}` rendered as escaped text — fine, but worth noting the address travels in cleartext through the admin mailbox. Not a template issue.
- Unsubscribe: internal admin notification, exempt.

### `admin_cost_alert.html` — internal alert (admin-only)
- Variables escaped: `cost_usd`, `day`, `threshold`, `kill_switch_status`, `app_url`, `row.feature`, `row.cost`. **OK.**
- Unsubscribe: internal admin notification, exempt.

### `unsubscribe_confirmation.html` — receipt (transactional)
- No interpolated variables. **OK.**
- Unsubscribe: confirmation email, exempt.

### `newsletter_confirm.html` — double-opt-in (promotional precursor)
- Variables escaped: `segment_label`, `frequency_label`, `confirm_url`. **OK.**
- Extends `base.html` — inherits footer unsubscribe guard.
- **LOW:** No unsubscribe link is needed in a confirm-opt-in email (user has not yet been added), but if `unsubscribe_url` is not passed the footer just omits the link — which is correct here.

### `referral_reward.html` — payout-style notice
- Variables escaped: `display_name`, `referred_email`, `reward_label`, `next_milestone`, `total_converted`, `next_reward_label`, `referrals_url`. **OK.**
- **LOW:** `referred_email` rendered as escaped text body content — fine for HTML but exposes one user's email inside another user's mailbox. Not a template defect (intentional product behaviour), but worth flagging for any compliance review.
- Unsubscribe: arguably promotional (rewards re-engagement). Footer link depends on caller passing `unsubscribe_url`. **MED** — see HIGH #2.

### `market_resolved.html` — engagement / digest-adjacent
- Variables escaped: `market_question`, `outcome`, `total_count`, `correct_count`, `market_url`. **OK.**
- **MED:** Promotional / engagement category. Footer unsubscribe depends on caller. See HIGH #2.

### `admin_subscription_drift.html` — internal alert (admin-only)
- Variables escaped: `drift_count`, `total_count`, `drift_pct`, `app_url`. **OK.**
- Unsubscribe: internal, exempt.

### `admin_forensic_alert.html` — internal alert (admin-only)
- Variables escaped: `timestamp`, `admin_email`, `target_watermark`, `target_user_id`, `ip_address`, `user_agent`. **OK.**
- `{{ user_agent }}` is the most adversarial field (attacker-controlled). It is escaped, so safe in HTML, but renders inside `<code>`-style cell with `word-break:break-all`. **OK.**
- External link: `https://narve.ai/admin/audit` hardcoded. **OK.**
- Unsubscribe: internal, exempt.

### `market_mover_alert.html` — promotional alert (standalone, no `extends`)
- **MED:** Standalone template — bypasses `base.html`. Ships its own `<html>` skeleton and footer.
- **MED:** Loads `<img src="{{ app_url }}/_gateway_static/img/logo.png">` — first-party but functions as an open-tracker pixel. See Top-3 #3.
- **MED (unsubscribe wiring):** Footer emits `<a href="{{ unsubscribe_url }}">` with no `{% if %}` guard. If caller omits the key, the rendered link is `<a href="">` which navigates to the email's own URI. CAN-SPAM/GDPR compliance hinges on caller discipline.
- Variables escaped: `market_title`, `price_change_display`, `lookback_hours`, `current_price`, `previous_price`, `top_source.handle`, `top_source.credibility`, `top_source.direction`, `top_source.days_ago`, `app_url`, `unsubscribe_url`, `watermark`. **OK** on escaping.
- `{{ watermark_zw }}` is rendered inside `<span style="font-size:0;line-height:0;">` — invisible per-recipient watermark, escaped. **OK.**
- Inline expression `{% if price_change > 0 %}` — note the renderer's `_if_sub` regex only supports truthy on bare identifiers (`r"\{%\s*if\s+([\w\.]+)\s*%\}"`). The expression `price_change > 0` will not match this regex (contains a space + operator), so this `{% if %}` block is **never substituted** — the literal `{% if ... %}...{% endif %}` text will appear in the rendered HTML. **LOW (bug, not security):** colour swap for positive/negative price change does not work; both branches fall through to the literal output. (Possibly handled upstream as `price_change_display` already.)
- Inline JS / `mailto:`: none.

### `enquiry_notification.html` — inbound contact form
- Variables escaped: `enquiry_email`, `job_title`, `message`. **OK.**
- `{{ message }}` renders inside `<td>` with `white-space:pre-wrap` — HTML-escaped, line breaks preserved. **OK.**
- **LOW:** `enquiry_email` is attacker-supplied (anyone can submit the contact form). It is escaped on render, so no XSS. The corresponding SMTP `Reply-To` header is set in `public_routes.py`, not in the template — out of scope, but worth a separate Reply-To validation review.
- Unsubscribe: internal recipient (operator inbox), exempt.

### `weekly_intelligence.html` — promotional digest
- Variables escaped: `stats.period_start`, `stats.period_end`, `stats.hero_number`, `stats.hero_label`, `s.source`, `s.category`, `s.content`, `s.credibility`, `stats.summary`, `app_url`, `pdf_path`. **OK.**
- **MED:** Promotional. Extends `base.html`; footer unsubscribe depends on caller passing `unsubscribe_url`. See HIGH #2.

### `winback_30d.html` — promotional re-engagement
- Variables escaped: `display_name`, `app_url`. **OK.**
- **MED:** Promotional. Footer unsubscribe depends on caller.

### `winback_7d.html` — promotional re-engagement
- Variables escaped: `display_name`, `app_url`. **OK.**
- **MED:** Promotional. Footer unsubscribe depends on caller.

### `payment_failed.html` — billing notice (transactional)
- Variables escaped: `app_url`. **OK.**
- Unsubscribe: transactional / dunning, exempt.

### `password_reset.html` — security reset (transactional)
- Variables escaped: `reset_url` (used in both `href` and visible text). **OK.**
- Unsubscribe: transactional, exempt.

### `incident_update.html` — status-page subscriber
- Variables escaped: `incident_title`, `update_status`, `update_message`, `affected_components`, `severity`, `status_url`. **OK.**
- Unsubscribe: status subscriptions have their own opt-out path (`/status/unsubscribe?token=...` per `status_routes.py:314`). Footer renders if `unsubscribe_url` is passed.
- **LOW:** `{{ update_message }}` is admin-supplied free-form text inside a styled `<p>`. Escaped, so safe. No richtext support means admins cannot inject markup — verify this matches operator expectations.

### `affiliate_payout_threshold.html` — affiliate notification
- Variables escaped: `display_name`, `pending_gbp`, `threshold_gbp`, `dashboard_url`. **OK.**
- **LOW:** Affiliate-related; arguably promotional. Footer unsubscribe depends on caller.

### `account_deletion_confirmation.html` — GDPR notice (transactional)
- Variables escaped: `deletion_date`. **OK.**
- Unsubscribe: GDPR confirmation, exempt.

### `account_deleted.html` — GDPR notice (transactional)
- No interpolated variables. **OK.**
- Unsubscribe: GDPR confirmation, exempt.

### `subscription_cancelled.html` — billing notice (transactional)
- Variables escaped: `period_end_date`, `app_url`. **OK.**
- Unsubscribe: transactional, exempt.

### `incident_resolved.html` — status-page subscriber
- Variables escaped: `incident_title`, `update_message`, `affected_components`, `severity`, `status_url`. **OK.**
- Unsubscribe: status subscriptions have their own opt-out path.

### `saved_prediction_resolved.html` — engagement
- Variables escaped: `display_name`, `source_handle`, `prediction_text`, `outcome`, `user_note`, `saved_url`. **OK.**
- **LOW:** `{{ user_note }}` is user-supplied text from the saved-prediction flow, here echoed back into that same user's inbox. Escaped. **OK.**
- **MED:** Engagement / promotional-adjacent. Footer unsubscribe depends on caller.

### `weekly_digest.html` — promotional digest
- Variables escaped: `week_start`, `week_end`, `subproduct_labels_str`, `p.source`, `p.category`, `p.content`, `p.credibility`, `s.handle`, `s.credibility`, `s.accuracy`, `app_url`. **OK.**
- `{{ watermark_zw }}` rendered inside `<span style="font-size:0;">` — escaped, invisible. **OK.**
- **MED:** Promotional. Footer unsubscribe depends on caller (see `jobs/email_jobs.py:289` which does inject `unsubscribe_url`). Acceptable in current state, but template-side enforcement is absent.

### `morning_briefing.html` — promotional digest (standalone, no `extends`)
- **MED:** Standalone — bypasses `base.html` footer.
- **MED:** Loads `<img src="{{ app_url }}/_gateway_static/img/logo.png">` — see Top-3 #3.
- **MED (unsubscribe wiring):** Footer emits `<a href="{{ unsubscribe_url }}">` with no `{% if %}` guard. If caller omits the key, link points to empty href. Same pattern as `market_mover_alert.html`.
- Variables escaped: `app_url`, `date`, `subproduct_labels_str`, `m.title`, `m.market_price`, `m.betyc_price`, `m.edge_display`, `m.source_count`, `s.source_handle`, `s.credibility`, `s.content`, `r.title`, `r.close_time`, `unsubscribe_url`, `watermark`. **OK.**
- **LOW (bug, not security):** `{% if m.edge > 0 %}` — same renderer regex limitation as `market_mover_alert.html`. Expression with operator is not parsed; the literal block falls through. Caller must precompute the colour or pass a flag.
- Inline JS / `mailto:`: none.

### `referral_invite.html` — cold introduction (promotional)
- **MED:** `{{ raw_token }}` is unescaped (same caveat as `token_delivery.html`).
- Other variables escaped: `referrer_display_name`, `app_url`. **OK.**
- **MED:** This email goes to a non-customer (cold introduction at the invitee's request, but still a marketing surface). It extends `base.html` so the footer unsubscribe shows up *if and only if* the caller passes `unsubscribe_url`. For an invite to a non-subscriber, the legal correct behaviour is to include a contact / opt-out for further messages. Current template-side: silent if caller omits.

### `newsletter_blast.html` — operator-published newsletter (promotional)
- **HIGH:** `{{ raw_body_html }}` is rendered **unescaped**. This is intentional — the renderer's `raw_` prefix is the explicit opt-out, and an admin newsletter publishing flow needs HTML. Risk inventory:
  - Admin-account compromise → fan-out phishing to entire subscriber list.
  - No CSP, no `<script>` filter, no allowlist of tags — the rendered output can contain arbitrary markup, including `<script>` (mostly ignored by mail clients) and tracker-style `<img>` tags.
  - Recommend (out of scope here, but flagging): wrap `raw_body_html` in a sanitiser (e.g. `bleach` allowlist) before passing to context; or move to an admin-only Markdown-source flow with server-side rendering and a strict tag allowlist.
- Other variables escaped: `subject`, `app_url`. **OK.**
- Extends `base.html`; footer unsubscribe depends on caller passing `unsubscribe_url`. See `public_routes.py:414` which does inject it for the newsletter flow — good in current state, fragile by template-design.

### `incident_created.html` — status-page subscriber
- Variables escaped: `incident_title`, `severity`, `incident_status`, `affected_components`, `description`, `status_url`. **OK.**
- Unsubscribe: status subscriptions have their own opt-out path.

### `webhook_disabled.html` — developer notification (transactional)
- Variables escaped: `display_name`, `consecutive_failures`, `webhook_url`, `app_url`. **OK.**
- `{{ webhook_url }}` is user-supplied (their own webhook URL) — escaped on render. **OK.**
- Unsubscribe: transactional, exempt.

---

## Cross-cutting observations

1. **Escape posture is sound.** The custom renderer escapes by default; only three call sites use `raw_*`: `raw_body_html` (newsletter), `raw_token` (token + referral invite). The first is HIGH; the latter two are MED hardening targets.
2. **No inline `<script>`, no `onclick` / `onload` / `onerror`, no `javascript:` URIs, no `mailto:` links** in any template. Reply addresses are set via SMTP `Reply-To` header from route code, not template-side — so no template-level mailto-injection risk.
3. **External images:** only the two standalone templates (`morning_briefing.html`, `market_mover_alert.html`) include an `<img>`, and only to `{{ app_url }}/_gateway_static/img/logo.png` — first-party. No third-party tracking pixels found. The first-party logo image, served per-recipient, still functions as an open-tracker for analytics purposes.
4. **Unsubscribe coverage is caller-enforced, not template-enforced.** Every promotional path inspected does pass `unsubscribe_url`, but the templates would render an empty-href link (or omit the link entirely) if a future caller dropped the key. A defensible hardening would be a renderer-level assertion (e.g. `assert unsubscribe_url in ctx` for templates tagged `promotional`) or a `{% require unsubscribe_url %}` directive.
5. **Renderer expression engine is limited to bare identifiers in `{% if %}`** — `{% if m.edge > 0 %}` in `morning_briefing.html` and `{% if price_change > 0 %}` in `market_mover_alert.html` are inert because the regex only matches `[\w\.]+`. The branches don't execute; the literal Jinja-style block falls through into the email body. Not a security issue, but operationally broken.

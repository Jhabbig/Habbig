# gateway/static/ — Gateway HTML, CSS, and JS

The browser-facing surface of the gateway. Every apex page (landing,
pricing, signup, login, profile, billing, admin, ...) lives here as a
plain HTML file served by FastAPI's `StaticFiles` mount, plus the shared
CSS theme and the small JS modules that power live pricing, the
account/dashboard switcher, and the in-app trade widget.

These files are the **only** thing the public internet sees from the
gateway — everything else is JSON APIs called by `fetch` from the JS in
this directory.

## HTML pages

Each page is a self-contained `<html>` document. They share styling via
`gateway.css` + `narve-theme.css` and behavior via `switcher.js`.

| File | Purpose |
|---|---|
| `landing.html` | Marketing front page at `narve.com/`. Pitches the suite, CTA into signup/pricing. |
| `pricing.html` | Public pricing grid — per-dashboard tiers, bundle discount, Stripe checkout buttons. |
| `enquire.html` | Contact / sales-enquiry form (for users who want a custom plan or have questions before signing up). |
| `signup.html` | Email + password registration form. POSTs to `/auth/register`. |
| `login.html` | Email + password sign-in form. POSTs to `/auth/login`. |
| `gate.html` | "You need to log in / subscribe to view this dashboard" interstitial shown when an unauthenticated user hits a protected subdomain. |
| `forgot-password.html` | Request a password-reset email. POSTs to `/auth/forgot-password`. |
| `reset-password.html` | Land here from the reset email — sets a new password via `/auth/reset-password`. |
| `dashboards.html` | Logged-in landing — grid of dashboards the user has access to, with deep links into each subdomain. |
| `subscribe.html` | Per-dashboard subscription / upgrade flow. Lists plans, hands the user off to Stripe Checkout. |
| `billing.html` | Stripe-managed billing portal redirect + invoice history. |
| `account.html` | Account overview — email, plan, subscription status, danger-zone delete. |
| `profile.html` | Editable profile fields (display name, avatar) + linked auth providers. |
| `settings.html` | User preferences (notifications, default landing dashboard, unit system). |
| `support.html` | Support / help-center page with contact info and FAQs. |
| `admin.html` | Admin-only console — list/search users, grant or revoke subscriptions, view error logs. Backend enforces admin role. |
| `suspended.html` | Shown when a user's account is suspended (failed payment, abuse). |
| `impressum.html` | Legal "Impressum" page (required for German/EU operation) — company details, contact, hosting. |
| `preview.html` | Internal "preview the dashboards in an iframe" page used for marketing screenshots and design QA. |

## Stylesheets

| File | Purpose |
|---|---|
| `gateway.css` | The main stylesheet — layout, components, forms, buttons, tables, modals. ~35 KB. |
| `narve-theme.css` | Brand theme layer — Narve colors, typography, dark/light tokens. Loaded after `gateway.css` so it can override. |

## JavaScript modules

Plain ES modules — no bundler. Loaded with `<script type="module">`.

| File | Purpose |
|---|---|
| `switcher.js` | The top-of-page dashboard switcher dropdown + auth/subscription state poller. Reads `/auth/me`, dispatches custom events when the session changes, and renders the cross-dashboard nav on every page. |
| `sse-client.js` | Tiny `EventSource` wrapper used by pages that subscribe to live server-sent events (price ticks, alert notifications). Handles reconnection with backoff. |
| `trade.js` | The in-page trade widget for placing Polymarket CLOB orders from inside the gateway shell — order form, balance display, confirmation modal. Talks to the gateway proxy endpoints, not directly to Polymarket. |

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `dummy/` | Single placeholder `index.html` — used as a fallback `root_path` target during local dev. | `dummy/README.md` |
| `img/` | Static image assets (logo, headshots). | `img/README.md` |

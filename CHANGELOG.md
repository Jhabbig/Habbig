# Changelog

All notable user-visible changes to narve.ai are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is date-based (no semver yet); releases correspond to
deploy commits on `feature/platform-build`.

## Week of 2026-05-14

### Added

- **Voters Atlas** (`voters.narve.ai`) — election and polling data
  across 27 countries, integrated with the V-Dem democracy index for
  long-horizon governance signals.
- **Climate Change** (`climate.narve.ai`) — NOAA CO2 / CH4 / SST /
  ENSO + NASA GISTEMP + NSIDC sea-ice indicators with rolling
  12-month forecasts.
- **Eco Disasters** (`disasters.narve.ai`) — live disaster feed
  fusing NASA EONET, USGS earthquakes, GDACS, NASA FIRMS, and
  ReliefWeb into a single map + timeline.
- **Whale Watch** (`whale.narve.ai`) — SEC EDGAR 13F / 13D / Form 4
  ingest tracking 47 institutional investors with quarterly delta
  views and concentration metrics.
- **Central Bank Tracker** (`cb.narve.ai`) — FRED + ECB SDW + BoE
  rate series with implied-path forecasts derived from market data.
- **World Health** (`health.narve.ai`) — WHO outbreak alerts, FDA
  drug shortages, and antimicrobial resistance (AMR) trend panels.
- **Love Atlas** (`love.narve.ai`) — 30 macro relationship metrics
  spanning marriage / divorce / fertility / cohabitation.
- **Annoyance Happiness view** — ternary polarity classifier that
  splits sentiment into positive / neutral / negative buckets and
  visualises the mix over time.
- **`/settings/integrations`** — Polymarket wallet, Kalshi API
  token, and bankroll management consolidated under one settings
  surface.
- **`/settings/trading-addon`** — Kelly-criterion sizing config,
  per-position risk limits, and auto-execute toggle (gated behind
  the trading add-on).
- **`/admin/health-monitor`** — single-pane status board for all
  13 services with uptime, latency, and last-error visibility.
- **`/api/docs`** — public API reference rendered from the OpenAPI
  spec served at `/api/openapi.json`.
- **`/changelog.rss`** — subscribe to release notes via RSS.
- **Per-recipient email watermarks** on Pro intelligence emails —
  forensic trace for leak investigation.
- **Spanish (es) + Brazilian Portuguese (pt-br) + German (de)** —
  262 keys each, native-quality translations across the entire
  product surface.
- **Web push notifications** — subscribe / unsubscribe / test flow
  with permission-gated UI.

### Changed

- **Typography — monospace.** `var(--font-mono)` switched from
  `SF Mono` to **Geist Mono** as the brand monospace face.
- **Default share card.** Pages without a per-page `og:image` now
  fall back to a default narve `og:image`; every one of the 13
  subproducts ships its own per-subdomain OG card.
- **Focus styles — mouse-suppress.** Site-wide `:focus` rules
  migrated to `:focus-visible`. Mouse clicks no longer leave a
  lingering ring; keyboard navigation still gets the full ring.
- **Subscription welcome email** is now subproduct-aware — three
  variants tailored to the user's primary subproduct.
- **Weekly digest + morning briefing** filtered to the
  subproducts each user actually subscribes to.
- **Admin login** now redirects to `/gate` (previously `/token`).
- **`requirements.lock`** regenerated from prod Python 3.12 —
  prior lockfile was built on Python 3.9 and was missing the
  CVE-patched `cryptography` release.

### Security

- **Permissions-Policy expanded** — default-deny on camera,
  microphone, payment, USB, MIDI, bluetooth, and every other
  optional browser API.
- **`Cross-Origin-Resource-Policy: same-origin`** added — blocks
  cross-origin reads of narve responses.
- **HMAC `X-Gateway-Secret` middleware** enforced on all 7 new
  subproduct subdomains — direct hits to subproduct ports without
  the gateway header are rejected.
- **127.0.0.1 bind** on subproduct services (previously `0.0.0.0`)
  — eliminates LAN exposure even if a firewall rule drifts.
- **`/api/push/subscribe` host allowlist** — only FCM, Mozilla,
  Apple, and WNS endpoints accepted as subscription targets.
- **`world-health-dashboard`** RSS parsing migrated to
  `defusedxml` — eliminates the XXE class of attacks.
- **`love-dashboard`** innerHTML interpolations escaped — defuses
  XSS via poisoned third-party API responses.
- **CSRF middleware** now inspects PATCH / PUT / DELETE (behind a
  soft-warn flag so we can monitor before enforcing).
- **CSRF allowlist tightened** — removed the broad
  `/api/scraper/*` prefix bypass.
- **Stripe webhook hardening** — idempotency keys + signature
  checks tested against real-world replay attempts.
- **Audit-log forensic alerting** wired up at
  `/admin/trace-watermark`.
- **Stale `gateway/requirements.lock`** removed (it pinned the
  CVE-vulnerable `cryptography 44.0.1`).
- **Subscription cancellation email** kwarg bug fixed — was
  silently failing to send.
- **Polymarket wallet-connect now requires SIWE (EIP-4361)
  signature** — the connect endpoint formerly accepted any
  0x-prefixed address with zero proof of key ownership, letting an
  attacker attach a victim's public wallet to their own account and
  harvest positions through portfolio sync. The new flow issues a
  one-time nonce, asks the wallet to `personal_sign` a canonical
  SIWE message pinned to `narve.ai` + chain id 1, and recovers the
  signer server-side via `eth_account`. Mismatched signers, replayed
  nonces, stale nonces (>5 min), and tampered domains are all 400s.
  Unsigned `wallet_address` POSTs are still accepted for a 30-day
  migration window and flagged `verified=false` in the response;
  clients must roll over to the signed flow before the cutoff.

### Fixed

- **`get_invite_token`** no longer hardcoded to filter
  `status='unclaimed'` — claimed and revoked invites were breaking
  session validation downstream.
- **`/api/embed/best-bets`** no longer issues N+1 queries — 61
  queries collapsed to 2, with a 120 s response cache layered on
  top.
- **4 hot routes** (`/dashboards`, `/settings`, `/signal-search`,
  `/sources/{handle}`) now cache their DB reads.
- **Sync Stripe calls** (`stripe.Subscription.retrieve()` and
  `stripe.checkout.Session.create()`) wrapped for async execution
  — no more event-loop block on payment paths.
- **Admin unbounded SELECTs** paginated (`list_all_users`,
  `list_invite_tokens`, `list_all_subscriptions`).
- **49+ pre-existing test failures** resolved.
- **Migration filename mismatch** corrected
  (`175_trading_addon_settings.py` → `176_…`).

## [Unreleased]

Work in flight on `feature/platform-build` that hasn't been tagged.

### Added

- **Community Takes** on every market detail page — paid subscribers post
  YES / NO / Neutral takes with confidence + reasoning; anyone logged in
  can upvote / downvote; shadow-hide at downvotes ≥ 3 AND
  quality_score < −5 with in-app author notification on the edge transition.
- Public profile strip at `/u/{user_id}/takes` — top 5 takes by
  quality, gated on the existing leaderboard opt-in.
- Blended credibility badge next to each take author (0.85 × global
  accuracy + 0.15 × take accuracy).
- `/settings/takes` — user's own take history with correct / incorrect /
  hit-rate / average-quality tiles.
- `/admin/moderation` — admin queue for reported takes; cascade-closes
  sibling reports on delete.
- `/api/v1/forecasts/compare/{slug}` + `/api/v1/forecasts/providers` —
  market-detail comparison against Metaculus, Manifold, 538, Silver
  Bulletin with real Brier scores at `/dashboard/models`.
- Collections — Spotify-style playlists for markets / sources /
  predictions with typeahead, share, RSS, profile section, typeahead
  add-widget on detail surfaces.
- Scenarios — conditional probability + correlation matrix (Pro).
- Share loop + referral rewards — share buttons on market / source /
  prediction pages, OG cards, daily retention cron, invite-replenish
  monthly job.
- ⌘K command palette with FTS snippet highlight, `@` prefix for
  sources, `Cmd+1..9` tab jump, "popular" empty-state, abortable
  search, clear-recent-searches.
- Public API v1 — keyed Bearer-auth dev API with sources / predictions
  / consensus / edge endpoints; new `/api/version` and full OpenAPI docs.
- Saved views — pinned sidebar + shared-view banner across 4 user
  tabs.
- WebSocket infra — single `/ws` endpoint, 5 channels, hub pub/sub +
  after-broadcast hook.
- In-app notification bell (migration 026) with preference gating +
  SSE fan-out.
- Onboarding tour + first-week goals + admin metrics (migrations
  090-091) + first-run sample-feed loader.
- Engagement tracking, churn detection, 3-step cancel-retention flow.
- Feedback board + roadmap voting + admin triage (migration 130) with
  rate-limit, self-vote block, "mine" filter, similar-items,
  bulk-admin tools, monthly digest.
- i18n scaffold — language switcher UI, client-side `t()`, Intl
  helpers, landing-page conversion, extended extractor + JSON-body
  bridge.
- PWA install-app banner (login-gated).
- Multi-jurisdiction compliance scaffold on `/terms` + `/privacy`.
- Chrome extension, portfolio sync (Polymarket + Kalshi), Telegram +
  Discord bots, insider signals, environmental-impact panel, weekly
  reports, market-movement alerts, Claude-based prediction extractor
  (migrations 050–059).

### Changed

- Admin dashboard: monochrome cleanup, new-tab links, shared app-shell
  chrome across 3 admin detail pages + `admin_affiliates.html` +
  `admin_status.html`.
- `predictions_public` + `predictions_history` + `saved` + `notifications`
  + `predictions` + `settings_billing` + `settings_embeds` +
  `settings_privacy` all rebuilt on the shared chrome.
- Scenarios, command-palette, sharing dashboard, and takes surfaces
  passed through `/design-critique` — a11y + keyboard nav + focus
  styles + sticky modal actions + empty-state CTAs.
- Database reorganisation — 246 queries extracted into per-domain
  `gateway/queries/`; 46 routes extracted from `server.py` into 4
  feature modules.
- `takes` row copy: `8/10` → `conf 8`; post button `Post your take +`
  → `Post take`; sort labels `By quality` → `Quality`.
- Scheduler centralised on APScheduler with `/admin/jobs` UI
  (migration 105).

### Fixed

- Forecast Brier score now computed from real provider data instead of
  placeholder.
- Sharing metrics FOUC on window-tab switch; sparkline a11y
  (role="img" + aria-label with peak/recent); country card demoted to
  2-col width; focus-visible outline on window tabs.
- Schema drift in `market_snapshots` columns re-declared (migration
  095).
- Waitlist advances +1 place per referral (bug: previously +5 silently
  miscounted).
- Email template cache invalidation on admin edits.
- TTL cache integration on hot read paths (api_v1 sources, consensus,
  edge) with `on_subscription_change` invalidation.

### Security

- **AUDIT #4** closed — 0C / 3H / 4M / 6L findings all remediated.
- **AUDIT #3b / #3c** closure — CVE dependency bumps (starlette,
  pillow, requests, python-dotenv, filelock) and duplicate migration
  de-collision.
- 2FA module fully removed (routes, templates, admin gate, settings
  card) after broken-feature assessment; see migration 019.
- Input hygiene: `POST /api/v1/markets/{slug}/takes` hardened with
  max-length + markdown strip on reasoning; self-vote blocked at the
  DB layer; bulk-report idempotency via unique index.
- Forensic watermarking: visible overlay + canvas steganography +
  per-response numeric signing + capture detection on the client +
  email alerts on admin-side detection.
- Claude cost controls (migration 074) — daily-spend job, kill switch,
  alert thresholds.

---

## Release cadence

No calendar cadence yet; releases are operator-triggered via the
deploy procedure in [RUNBOOK.md](RUNBOOK.md). Each deploy commits on
the server under a `deploy: <summary>` message; the local branch may
be at a different SHA depending on push policy at the time. Tagged
releases will begin once the public API v1 contract is frozen.

---

## Older history

The branch pre-dates this CHANGELOG; commit messages carry the full
history. `git log --since="60 days ago" --oneline` reproduces the
current-version entries; older changes are documented in-line in
their commit messages.

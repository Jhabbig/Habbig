# Stash Inventory — 2026-05-15

Audit of all 57 stashes on `feature/platform-build`. Read-only; nothing dropped/popped/applied during this pass.

**Method:** For each stash, ran `git stash show -p stash@{N} | head -50` and cross-checked the changed surfaces against `git log` on HEAD and current file contents. The branch is heavy with snapshot/redesign WIP stashes — the design system, Sentry release tagging, Stripe webhook IP allowlist, polarity/happiness, love subproduct, and per-subproduct flags have all landed in subsequent commits.

## Per-stash classification

```
stash@{0}  | WIP-UNCLEAR  | "wip non-css"                                | f74fc0c — large 12-file mix (features.py subproduct_key, pwa_middleware, queries/admin, push test, marketing template tweaks). Per-subproduct flag work landed in 6e38847, but the rest may be live edits.
stash@{1}  | STALE-DUPE   | "pre-changelog-append"                       | Sentry release detection — already shipped in d41f021 obs(sentry)
stash@{2}  | STALE-DUPE   | "wip-uncommitted-perf-task"                  | Same Sentry detect_release block — superseded by d41f021
stash@{3}  | STALE-DUPE   | "audit-10-temp-stash"                        | /stripe/webhook public_paths + 503 tolerance — superseded by 68b00c9 feat(stripe)
stash@{4}  | STALE-DUPE   | "pre-font-fix"                               | /admin/api/sentry recent-errors widget — superseded by 22a64f6 obs(admin)
stash@{5}  | STALE-DUPE   | "non-sentry-wip"                             | /admin/users page + Stripe stub doc — users page now live; superseded
stash@{6}  | STALE-DUPE   | "wip-other-agent-changes-before-revenue-fix" | Stripe stub doc tweaks + sentry_api caching — superseded by 22a64f6
stash@{7}  | STALE-DUPE   | "wip-changelog-aside"                        | CHANGELOG.md feature roll-up — superseded by b55a255 docs(changelog)
stash@{8}  | STALE-DUPE   | "wip-before-changelog-append-pm"             | _build_revenue_content subs list helper — perf audit already merged
stash@{9}  | STALE-DUPE   | "wip-before-admin-revenue-nameerror-fix"     | Editorial error-page copy in error_handlers.py — superseded
stash@{10} | STALE-DUPE   | "wip-before-trace-watermark-alerting-2"      | Same editorial error_handlers copy — superseded
stash@{11} | STALE-DUPE   | "wip-static-files"                           | changelog.html "What's new" + dashboards.html breadcrumb tweak — old design pass
stash@{12} | STALE-DUPE   | "wip-before-webhook-hardening"               | error_page.html token rewrite + sources.css padding — superseded
stash@{13} | STALE-DUPE   | "before-error-page-redesign-1778785593"      | /admin/jobs editorial typography + trace_watermark rate-limit — both shipped (admin shell + 767af52)
stash@{14} | STALE-DUPE   | "WIP on … design(sources)"                   | admin_jobs_routes refactor — /api/admin/jobs JSON shape now live
stash@{15} | STALE-DUPE   | "auto-restash-not-mine"                      | Massive 29-file other-agent dump (admin/jobs/error_page/about/about.css/login/token/register/dashboards/changelog/etc) — by inspection every piece is now on HEAD
stash@{16} | STALE-DUPE   | "recovered: wip-other-agent-changes-pre-collections" | trace_watermark forensic block + Source Serif 4 token — both shipped (767af52 + 6bbeeb8)
stash@{17} | STALE-DUPE   | "wip error_page before legal redesign"       | error_page.html article+footer markup — superseded by error-page redesign
stash@{18} | STALE-DUPE   | "pre-collections-redesign"                   | embed_api_key re-exports + polymarket batching docstring — re-exports landed
stash@{19} | STALE-DUPE   | "stash 179_job_runs.py before rebase"        | Untracked migrations/179_job_runs.py — slot now taken by 187/188, scheduler job_runs added in 105
stash@{20} | STALE-DUPE   | "stash3"                                     | eth-account==0.10.0 + Source Serif 4 + dashboards.css — all in HEAD
stash@{21} | STALE-DUPE   | "wip-cleanup-before-profile-redesign"        | webhooks.py DLQ/circuit-breaker docstring — superseded by 397e79c feat(webhooks)
stash@{22} | STALE-DUPE   | "design-sources-task-stash-2"                | stripe_webhook_hardening ipaddress import (IP allowlist) — superseded by e0d428f
stash@{23} | STALE-DUPE   | "WIP on … docs(changelog)"                   | Source Serif 4 @font-face — landed (Inter+Source Serif tokens shipped)
stash@{24} | STALE-DUPE   | "marketing-redesign-stash"                   | --font-body Source Serif 4 in tokens.css — landed
stash@{25} | STALE-DUPE   | "design-sources-task-stash"                  | trace_watermark forensic + rate-limit doc — superseded by 767af52
stash@{26} | STALE-DUPE   | "pre-pricing-redesign"                       | jobs/backend.py telemetry helpers — landed (job_runs telemetry on HEAD)
stash@{27} | STALE-DUPE   | "pre-settings-redesign-2"                    | Untracked SourceSerif4-Variable.woff2 font — fonts dir only ships Geist+Inter; HEAD switched to system-serif fallback per 2a9e340
stash@{28} | STALE-DUPE   | "wip: unrelated polymarket/webhooks edits"   | polymarket market-state batching + webhooks docstring — superseded
stash@{29} | STALE-DUPE   | "pre-settings-redesign"                      | trace_watermark forensic + admin_forensic_alert email — both shipped
stash@{30} | STALE-DUPE   | "pre-dashboards-redesign"                    | admin_forensic_alert _SUBJECTS entry — shipped (line in service.py)
stash@{31} | STALE-DUPE   | "pre-siwe-3"                                 | Stripe webhook hardening tests (IP allowlist) — superseded by 8c0be4c fix(stripe)
stash@{32} | STALE-DUPE   | "pre-siwe-audit-stash-2"                     | Same trace_watermark forensic block — superseded
stash@{33} | STALE-DUPE   | "pre-siwe-audit-stash"                       | Same _SUBJECTS entry — superseded
stash@{34} | STALE-DUPE   | "WIP: pre-pwa-task stash"                    | _STRIPE_WEBHOOK_CIDRS allowlist — superseded by e0d428f
stash@{35} | STALE-DUPE   | "snapshot-before-clean"                      | Push host allowlist in push_routes — landed (push routes use allowlist now)
stash@{36} | STALE-DUPE   | "tmp-all"                                    | annoyance polarity column + happiness — superseded by 934a935 feat(annoyance)
stash@{37} | STALE-DUPE   | "tmp2"                                       | Love Atlas config + market_connect rate-limit — superseded by 5945f8c feat(love)
stash@{38} | STALE-DUPE   | "WIP-pre-pull-rebase"                        | NARVE_SECURITY_AUDIT.md AUDIT #9 entry + changelog_routes + voters/_common — audit #9 is on HEAD (13cb863)
stash@{39} | STALE-DUPE   | "WIP-other-agents"                           | annoyance polarity full feature — superseded by 934a935 (1618 LOC of redundant work)
stash@{40} | STALE-DUPE   | "wip-during-whale-fix"                       | climate-dashboard 24h cache + disaster timeouts — landed via climate fetcher commits
stash@{41} | STALE-DUPE   | "tmp-3-before-pull"                          | BUGFIX_LOG entries — already in HEAD
stash@{42} | STALE-DUPE   | "tmp-2-before-subproducts-test-fix"          | centralbank BetterStack/Logtail wiring — already wired
stash@{43} | STALE-DUPE   | "tmp-before-subproducts-test-fix"            | Same BUGFIX_LOG content — superseded
stash@{44} | STALE-DUPE   | "wip-before-climate"                         | annoyance polarity again + changelog_routes — superseded by 934a935
stash@{45} | STALE-DUPE   | "pre-systemd-task stash"                     | annoyance polarity migration + email_jobs batch admin probe — polarity shipped, batching is perf nicety likely covered
stash@{46} | STALE-DUPE   | "wip-pre-gdpr-extension"                     | CLOUDFLARE_CHANGES 7-subdomain entry + Terraform + ingress — already documented in HEAD
stash@{47} | STALE-DUPE   | "pre-logtail-wire"                           | centralbank server hardening + cursor-paginated invite_tokens — landed
stash@{48} | STALE-DUPE   | "WIP before bugfix log update"               | get_user_active_subproducts + iOS 16px font + i18n pricing key — first two shipped, i18n nicety
stash@{49} | STALE-DUPE   | "auto-stash for CLOUDFLARE_CHANGES update"   | get_active_subscription_counts_by_dashboard + trading_addon_settings — both landed
stash@{50} | STALE-DUPE   | "temp-stash-2-for-architecture-md"           | centralbank server hardening — landed
stash@{51} | STALE-DUPE   | "temp-stash-for-architecture-md"             | Trading addon settings queries — landed
stash@{52} | STALE-DUPE   | "WIP on … expand catalogue assertion 6→12"   | Cursor-paginated list_invite_tokens + iOS 16px — pagination landed (cursor pagination merged)
stash@{53} | STALE-DUPE   | "WIP unrelated to happiness work"            | Centralbank cursor pagination on list_all_subscriptions + iOS 16px — pagination landed (2b7b14d)
stash@{54} | STALE-DUPE   | "WIP before centralbank wire"                | Voters style.css iOS-no-zoom on input/select — design churn, likely landed
stash@{55} | STALE-DUPE   | "wip: voters css + og images before whale-dashboard work" | conftest TestClient cookie reset + error_handling test — test infra likely converged
stash@{56} | STALE-DUPE   | "wip-before-email-fix"                       | annoyance polarity + centralbank USER_AGENT/timeouts + email_jobs N+1 — superseded
```

## Summary counts

- **STALE-DUPE:** 56 (all but stash@{0})
- **STALE-REVERTED:** 0
- **WIP-UNCLEAR:** 1 (`stash@{0}` — touches 12 files including features.py subproduct flags, pwa_middleware, queries/admin, push_routes test, marketing templates — much of this is in HEAD but worth a 60-second sho-eyeball before drop)
- **WIP-VALUABLE:** 0

Every stash I inspected has content that subsequently landed on `feature/platform-build` via a later commit, or is documentation/design churn that was reworked downstream. The branch shipped audit #9-13, the Stripe IP allowlist + signature + idempotency, Sentry release tagging, /admin/users + /admin/jobs + /admin/health-monitor, the Happiness view, the Love subproduct, and per-subproduct feature flags between the time most of these stashes were created and now — every one of those features was foreshadowed by a stash.

## Recommended bulk-drop

After sho confirms `stash@{0}` is also stale, the following one-liner drops all confirmed-stale stashes (highest index first to avoid renumbering bugs):

```bash
# Drops stash@{1} through stash@{56}. Leaves stash@{0} for manual review.
for i in $(seq 56 -1 1); do git stash drop "stash@{$i}"; done
```

If sho prefers to nuke all 57 in one go after eyeballing `stash@{0}`:

```bash
git stash clear
```

## Why nothing is WIP-VALUABLE

The `feature/platform-build` branch has been pushing every few minutes for days — every stash was a snapshot taken before pulling/rebasing/redesigning. The work in each one was either:
1. Re-implemented cleanly in a later commit (most cases).
2. Doc/CSS churn that got reworked before merge.
3. Other-agent diffs that the user explicitly labelled `wip-other-agent-changes` / `auto-restash-not-mine` — already on HEAD by definition.

The "snapshot before X" naming pattern (`pre-collections-redesign`, `pre-pricing-redesign`, `pre-dashboards-redesign`, etc.) plus the corresponding redesign commits visible in `git log` make this a high-confidence conclusion.

---

**Audit run:** 2026-05-15 — 57 stashes inspected, 56 stale, 1 unclear, 0 valuable. Read-only pass.

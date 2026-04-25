# QA walkthrough — manual checklist

**Run before any deploy that touches > 5 files OR any user-facing
change.** Estimated time: 15–20 minutes.

The automated suite at `gateway/tests/qa/qa_walk_*.py` covers the
mechanical bits (route reachability, auth gating, font sourcing,
responsive breakpoints, perf headers, dark-mode tokens, Lighthouse
score). This checklist covers the things automation can't reliably
catch — eye-test, toast feel, copy quality, transition smoothness,
unexpected console errors during real navigation.

---

## A. Server boot — 1 min

- [ ] `tail -50 /tmp/gateway.log` (or wherever the prod log is) —
      no `ERROR` lines that aren't migration-related.
- [ ] `curl -I http://localhost:7000/` returns 200 and an
      `X-Response-Time-ms` header < 200 ms.
- [ ] `curl http://localhost:7000/health` returns 200 with `ok`.

If any of the above fails, stop. Don't deploy.

## B. Unauthenticated walk — 5 min

Open a fresh incognito window. For each path: does it render? Is the
Inter font in use? Is the header consistent?

- [ ] `/` (prerelease landing)
- [ ] `/pricing`
- [ ] `/about`
- [ ] `/how-it-works`
- [ ] `/methodology`
- [ ] `/faq`
- [ ] `/changelog`
- [ ] `/team`
- [ ] `/press`
- [ ] `/privacy`
- [ ] `/terms`
- [ ] `/dpa`
- [ ] `/status`
- [ ] `/leaderboard`
- [ ] `/404-does-not-exist` — verify themed 404 page (not raw FastAPI JSON)
- [ ] DevTools console — zero red errors per page

## C. Authenticated walk — 5 min

Log in as a test user.

- [ ] `/dashboards`
- [ ] `/saved`
- [ ] `/notifications`
- [ ] `/settings`
- [ ] `/settings/billing`
- [ ] `/settings/saved-views`
- [ ] `/settings/api-keys`
- [ ] `/settings/embeds`
- [ ] `/billing`
- [ ] `/profile`
- [ ] `/u/<your-handle>` (if profile enabled)
- [ ] `/intelligence` (or 402 if free tier — that's fine)
- [ ] `/signal-search`

For each: render, no console errors, breadcrumb correct, page subtitle
present, primary action visible.

## D. Admin walk — 3 min

Log in as admin. Verify admin shell renders, every section accessible.

- [ ] `/admin`
- [ ] `/admin/cache`
- [ ] `/admin/backups`
- [ ] `/admin/flags`
- [ ] `/admin/emails`
- [ ] `/admin/impersonations`
- [ ] `/admin/audit-log`
- [ ] `/admin/logs/errors`
- [ ] `/admin/logs/live`
- [ ] `/admin/sharing`
- [ ] `/admin/churn`
- [ ] `/admin/subproducts`
- [ ] `/admin/search-analytics`

Spot-check: open `/admin/users`, search for the test user, confirm
`is_admin=0`, click impersonate, confirm banner appears, click "End
impersonation", confirm banner disappears.

## E. Style spot-check — 3 min

For 3 random pages — open DevTools, inspect:

- [ ] Body computed `font-family` resolves to `Inter`.
- [ ] No `style="color:#..."` inline overrides on content tags
      (Elements tab, search). Tier badges with
      `data-allow-inline-color` are exempt.
- [ ] Tab through 5 inputs / buttons — focus rings visible (2 px
      outline, palette colour `var(--text-primary)`).
- [ ] No dropped requests in Network tab.

## F. Mobile — 2 min

DevTools device mode → iPhone 14 Pro (390 × 844).

- [ ] No horizontal scroll on `/`, `/dashboards`, `/pricing`.
- [ ] Sidebar collapses to hamburger; tap target opens drawer; X
      closes it.
- [ ] Tap targets feel right — buttons/links ≥ 44 px on at least
      a sample of 5 elements.
- [ ] Forms scroll properly when keyboard would open (no fixed
      bottom bar covering the input).

## G. Dark mode toggle — 1 min

- [ ] Toggle to dark → no white flash on page load (cookie/local
      storage primes the theme before first paint).
- [ ] Toggle back to light → no black flash.
- [ ] All text remains readable in both themes — no near-invisible
      grey on grey.
- [ ] Highlight/selection colour adapts to theme.

## H. Empty state check — 1 min

Use a fresh test account with no data:

- [ ] `/dashboard/feed` shows the empty state (not "Loading…"
      stuck spinner).
- [ ] `/saved` shows empty state with primary action.
- [ ] `/notifications` shows empty state.
- [ ] `/predictions` (or your equivalent) shows empty state.

Each empty state should: (a) explain what's missing, (b) offer a
single primary action.

## I. Toast / save check — 30 s

- [ ] Save any setting → toast appears bottom-centre, fades after
      ~3 s.
- [ ] Click any "Copy link" button → "Link copied" toast.
- [ ] Trigger an error (e.g. submit invalid form) → toast in error
      tone, persists until dismissed.

## J. Lighthouse spot-check — 2 min (optional, CI does this nightly)

Run `npx lighthouse http://localhost:7000/ --preset=mobile` from a
clean DevTools window.

- [ ] Performance ≥ 0.85
- [ ] Accessibility ≥ 0.85
- [ ] SEO ≥ 0.85

---

## Sign-off

| Field | Value |
|---|---|
| Tested by | _____________ |
| Date | _____________ |
| Build SHA | _____________ |
| Issues found | _____________ |
| Deploy approved | [ ] |

If anything in A–G fails, fix it and re-run that section. H–J failures
are alert-but-don't-block at the team's discretion (e.g. an empty
state regression on a low-traffic page is logable for next sprint).

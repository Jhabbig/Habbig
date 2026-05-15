# Audit — prerelease deploy needed?

**Date:** 2026-05-15
**Auditor:** automated curl + diff
**Scope:** Compare served https://narve.ai/ HTML against on-disk `gateway/static/prerelease.html` at commit `845fc24` (the 2026-04-29 rolled-back state).
**Method:** synchronous bash only. No file modification.

---

## Verdict

**DEPLOY NEEDED — YES.**

The production server is serving a newer build of the prerelease page that
includes the newsletter double-opt-in flow (commit `82d10bc`, "feat(newsletter):
segments + double-opt-in + frequency preference"). The on-disk file at
`gateway/static/prerelease.html` has been rolled back to commit `845fc24`
("revert(prerelease): roll back to 2026-04-29 state (e8eaa68)") but that
rollback has not been propagated to production.

To honour the rollback, the on-disk file must be deployed to the server.

---

## Source state

- Working tree HEAD: `dcee81e` (clean re: `gateway/static/prerelease.html`)
- Last commit touching the file: `845fc24` (2026-04-29 state revert)
- File on disk matches `845fc24` exactly (no uncommitted edits).
- 231 lines.

## Served state

- Curl URL: https://narve.ai/
- Bytes received: 30,861 (284 lines).
- Served via Cloudflare (`cdn-cgi/challenge-platform` shim present).
- Body structure derives from the same template (same `pr-*` class tree) but
  with extra elements absent from the rolled-back disk file.

## Structural deltas (served vs. disk)

The diffs below are NOT all "deploy this file" deltas — some are normal at-runtime
template injection. The load-bearing ones are flagged with **(DRIFT)**.

### Expected runtime expansions (NOT drift)

These are produced by `render_page()` / template substitution at serve time;
they will appear on EVERY served page regardless of which disk file is deployed.
They do not indicate the wrong file is on the server.

| # | Served | Disk | Note |
|---|--------|------|------|
| 1 | `gateway.css?v=0443ef09`, `components.css?v=dc289a76`, `pages/prerelease.css?v=d625987d` | `{{ static: gateway.css }}`, `{{ static: components.css }}`, `{{ static: pages/prerelease.css }}` | `{{ static: ... }}` substitution with cache-bust hash. |
| 2 | Extra `<link>` blocks for `skeletons.css`, `states.css`, `lang-switcher.css`, `changelog_widget.css`, `explain_popover.css`, `mobile-a11y.css`, `narve-polish.css`, `narve-redesign.css` | (absent) | Injected by global head template. |
| 3 | Extra `<script>` tags: `skeletons.js`, `i18n-client.js`, `lang-switcher.js`, `changelog_widget.js`, `explain_popover.js`, `command-palette.js`, `narve-app.js`, `shortcuts.js`, `shortcuts-discovery.js`, `feedback_button.js` | (absent) | Global head scripts. |
| 4 | Inline `<style>` block with token vars, app-shell grid, page-header rules | (absent) | Critical-CSS inline injected. |
| 5 | `<!--narve-pwa-head-->`, `<!--narve-og-default-->`, manifest link, theme-color meta, apple-mobile-web-app meta, og:image meta block | (absent) | PWA + OG defaults injected. |
| 6 | `<link rel="preload"` for `GeistMono-Variable.woff2`, Google Fonts (`Instrument Serif`, `Source Serif 4`) | (absent) | Font preload injection. |
| 7 | `<a class="narve-skip-link">`, `<button class="narve-hamburger">`, `<div class="narve-sidebar-backdrop">` at top of body | (absent) | App-shell navigation chrome injected globally. |
| 8 | `<script id="__NARVE_I18N__">` JSON blob + `window.LANG`/`window.SUPPORTED_LANGS` script | (absent) | i18n client payload. |
| 9 | Cloudflare `cdn-cgi/challenge-platform` IIFE at end of body | (absent) | Cloudflare CDN insertion. |
| 10 | `<meta name="viewport" content="...viewport-fit=cover">` | `<meta name="viewport" content="width=device-width, initial-scale=1.0">` | Viewport meta tag has been overridden somewhere in the served pipeline (could be from render_page or a middleware). Not file-level drift on its own. |

### Page content drift (DRIFT — confirms newer build is live)

These deltas are **inside the prerelease template body** and are NOT explainable
by runtime injection — they are extra hand-authored markup/logic that exists in
a newer version of `prerelease.html` but is absent from the `845fc24` disk file.

| # | Element | Served | Disk (845fc24) |
|---|---------|--------|----------------|
| **D1** | `<p id="pr-confirm-hint">` inside `.pr-success` block | **Present** (lines 133–136 of served): `"Check your inbox — we sent a confirmation link. You won't receive newsletter emails until you click it."` | **Absent** |
| **D2** | JS handler in `submitEmail()` success branch | **Present** (lines 259–264 of served): comment `// Surface the double-opt-in expectation. ...` + `var confirmHint = document.getElementById('pr-confirm-hint'); if (confirmHint) confirmHint.style.display = 'block';` | **Absent** — disk version goes straight from setting share links to `document.getElementById('pr-success').classList.add('visible');` |

Both D1 and D2 are the **newsletter double-opt-in feature** added in commit
`82d10bc`. They are not present in the `845fc24` rollback. Their presence in
the served HTML is conclusive evidence that the rollback has not been deployed.

The class names and other copy (`pr-line1`, `pr-line2`, `pr-soon`,
`pr-success-title`, `pr-position-block`, `pr-ref-row`, `pr-ref-hint`,
`pr-share-row`, headline "Stop guessing who's right." / "Start knowing.",
"coming soon.", "You're on the list.", referral copy, `Notify me` button label)
are identical in both versions — the structural skeleton matches. The drift
is exclusively the double-opt-in addition.

---

## Recommendation

Deploy `gateway/static/prerelease.html` (current on-disk, commit `845fc24`)
to the production server using the standard scp + restart procedure, then
commit on the server post-deploy. After deploy, re-curl https://narve.ai/
and verify that:

- `pr-confirm-hint` element is **absent** from served HTML.
- `confirmHint` JS branch is **absent** from served HTML.

All other deltas listed in the "Expected runtime expansions" table will
remain — they are not drift.

**Do NOT modify `prerelease.html`** — it is off-limits per user directive and
is already in the desired state.

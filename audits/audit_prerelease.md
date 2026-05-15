# Pre-release page audit — 2026-05-15

**Verdict: CLEAN**

Read-only check confirming the prerelease page has no regressions versus
the previous-save state.

## Checks

### 1. `gateway/static/prerelease.html`

- Working tree matches HEAD (no uncommitted changes). `git diff --stat`
  returns nothing for this file.
- Last commit touching it: `82d10bc 2026-05-14 17:20:39 +0100 feat(newsletter): segments + double-opt-in + frequency preference`.
- Untouched today.

### 2. `gateway/static/pages/prerelease.css`

- Working tree matches HEAD (no uncommitted changes).
- Last commit touching it: `6a6594b 2026-05-14 21:57:24 +0100 test(qa): fix 3 failures — redesign vocabulary + dvh fallback sweep`.
- Body rule (line 9-10) confirmed correct:
  ```css
  body { min-height: 100vh;
    min-height: 100dvh; background: var(--bg-void); color: var(--text-primary);
    font-family: var(--font-ui); display: flex; flex-direction: column;
    justify-content: center; align-items: center; overflow-x: hidden; }
  ```
  - Uses `var(--font-ui)` (Inter): present.
  - `100dvh` fallback after `100vh`: present.

### 3. `gateway/pwa_middleware.py` critical CSS

- Strict regex `body\s*\{[^}]*font-family` returns no matches in the
  whole file.
- Critical CSS block (lines 79-103) paints `html,body` for background,
  color, smoothing, `min-height: 100vh; min-height: 100dvh`, font-size,
  line-height — but explicitly **no** `font-family`. Inline comment at
  line 81-84 documents the intent:
  > Don't set body font in critical CSS — pages own their font choice.
- Working tree has uncommitted edits to this file, but the diff is
  unrelated to the body font-family rule. The diff swaps Google Fonts
  link tags for self-hosted `InstrumentSerif-Italic.woff2` +
  `SourceSerif4-Variable.woff2` preloads (audit LOW #2: privacy/CSP
  hardening). The critical CSS section is unchanged by this diff.
- Last commit touching it: `0421267 2026-05-14 21:19:23 +0100 fix(critical-css): drop body font-family from injected CSS — breaks prerelease`.

### 4. Production curl: `curl -s https://narve.ai/ | head -200`

All expected `<link>` references intact in served HTML:

```
<link rel="stylesheet" href="/_gateway_static/gateway.css?v=0443ef09">
<link rel="stylesheet" href="/_gateway_static/components.css?v=dc289a76">
<link rel="preload" href="/_gateway_static/fonts/Inter-Variable-subset.woff2"
      as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/_gateway_static/pages/prerelease.css?v=d625987d">
```

Order, hrefs, and cache-busting query strings match the source template.

## Findings

None. No drift detected. All four checks pass.

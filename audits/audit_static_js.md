# Adversarial Audit — `gateway/static/js/`

Scope: every `.js` file under `gateway/static/js/`. Threat surfaces examined per
file: `innerHTML`/`outerHTML` with non-static input, `eval`/`Function`
constructor, `fetch()` with user-controlled URLs, credential or token storage
in `localStorage`, `postMessage` origin checks, `document.write`, jQuery
`.html()`.

Severity rubric used below:
- **Critical** — exploitable as-is, attacker action → code execution or auth
  bypass.
- **High** — XSS / privilege escalation requires only an attacker-controlled
  upstream string that the audit shows is reachable.
- **Medium** — defensive depth is missing; exploit requires an additional bug
  elsewhere (e.g. server response trust) or a narrow precondition.
- **Low** — best-practice deviation; no realistic exploit path inside the
  app's threat model.
- **Info** — note for reviewers; not a finding.

Files audited: 11.

---

## `admin-shell.js`

- `innerHTML`/`outerHTML` writes: none.
- `eval` / `Function` constructor: none.
- `fetch()`: none.
- `localStorage`: none (no credential storage).
- `postMessage` handlers: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

Reads `data-active-route`, `href`, and `data-route` via `getAttribute`, and
sets only `aria-current` via `setAttribute`. No string passed to a sink that
parses HTML. `location.pathname` is only compared (`indexOf`, `===`), never
written into the DOM.

**Findings:** none. **Severity: clean.**

---

## `cmdk.js`

- `innerHTML` writes: extensive (lines 79, 230, 253, 280, 285, 291).
- `eval` / `Function` constructor: none.
- `fetch()` URLs: `/api/search?q=…` (line 149). The query string flows through
  `encodeURIComponent` and the path is a literal — not user-controlled at
  origin level. No SSRF.
- `localStorage`: stores recent search queries under `nv-cmdk-recent`
  (read 380, write 393) and a theme preference (`narve-theme`, line 440).
  No credentials, no tokens.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding C1 — server-trusted `<mark>` re-emit in `safeHighlight()` (Medium)

`safeHighlight()` (427–432) escapes the entire snippet, then unescapes the
two literal strings `&lt;mark&gt;` and `&lt;/mark&gt;` back into real `<mark>`
tags. This is safe **if and only if** the server's FTS5 `snippet()` output
never produces a `<mark>` substring inside attacker-influenced content. The
sequence is observed: a market title containing the literal text
`<mark>onclick=alert(1)//</mark>` (which a user-supplied market question could
in principle contain after upstream sanitisation drift) would survive the
escape → unescape pass and render as a real `<mark>` element. The `<mark>`
element itself does not execute script, so this is **Medium, not High** —
the worst case is style-injection / visual confusion, not XSS.

Mitigation observed in cmdk: the call site at `rowHtml()` (296) only invokes
`safeHighlight()` on `item.highlight`, which the server marks distinct from
`title`. The grouping is acceptable.

### Finding C2 — `stripTags()` regex tag-stripper (Low)

`stripTags()` (417) uses `/<[^>]+>/g` to strip HTML from `p.content` before
escaping. Inputs like `<img src=x onerror=` (no closing `>`) would survive,
but the result is **then passed through `escape()`** at row render (line 298),
so the regex limitation is defensive depth only. No realistic XSS.

### Finding C3 — `recentSearches` poisoning surface (Low)

`pushRecent()` (387) writes the raw user query into `localStorage` and
`readRecent()` (378) reads it back unverified before passing through
`escape()` at row render. A second user on the same browser could pre-seed
the recent list, but the value only reaches the DOM via `escape()`. No XSS;
worth a note for shared-device threat modelling.

**Severity: 0 Critical, 0 High, 1 Medium, 2 Low.**

---

## `command-palette.js`

- `innerHTML` writes: backdrop scaffold (135), result rows (385), hint
  strings (287, 313, 332, 342).
- `eval` / `Function` constructor: none.
- `fetch()` URLs: `/api/search/popular` (224), `/api/search?q=…&types=…`
  (302), `/api/search/click` (POST, 476). All paths are literal; query
  parameters are passed through `encodeURIComponent`. No SSRF.
- `localStorage`: `narve:cmdp:recents` (read 41, write 52). No credentials.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding CP1 — server-trusted `<mark>` re-emit in `renderHighlight()` (Medium)

Same pattern as cmdk.js C1, slightly different mechanism (103–106): escape,
then `split(esc(MARK_OPEN)).join(MARK_OPEN)`. The trust model assumes the
server only emits `<mark>` around safe pre-rendered FTS spans. If a stored
record's `title_html` field is ever populated from raw user input by an
endpoint that **also** includes `<mark>` text in the value (server-side
templating slip), this collapses into an XSS vector. The element is `<mark>`,
not `<script>` / `<img onerror>`, so the immediate impact is style injection
only, but a future server-side change that loosens the snippet contract
breaks this with no client-side warning.

### Finding CP2 — `item.title_html` rendered through `innerHTML` (Medium → conditional High)

`renderGroups()` (362–367) emits the result `<span>` via template string
interpolation: `<span ...>${titleHtml}</span>`. `titleHtml` is the output of
`renderHighlight(it.title_html)` when the server provides the `_html` field.
Audit assumption: the `/api/search` response's `title_html` /
`subtitle_html` fields contain ONLY server-rendered text with `<mark>` tags
around match spans. **This contract is not enforced at the JS boundary.**
If the server is ever changed to pass a richer `_html` payload (e.g. an HTML
link the designer wants rendered), this becomes a direct
attacker-controlled-string-into-innerHTML XSS. Treat as **Medium today,
High the moment the server contract changes.**

### Finding CP3 — `data.queries` (popular searches) only passed through `esc()` (clean)

Verified: popular queries come from `/api/search/popular`, are stored on
items as plain `title`, and reach the DOM via `esc(it.title)` (364). Clean.

### Finding CP4 — `keepalive: true` POST to `/api/search/click` with server-supplied `query_id` (Low)

`navigate()` (476) posts `{ query_id, result_type, result_id }` after the
user clicks a row. `query_id` is whatever the server returned in the last
search response; it's never re-validated. A malicious server response could
poison the analytics row, but that's a server-trust concern, not a JS bug.

**Severity: 0 Critical, 0 High, 2 Medium, 1 Low.**

---

## `first_week_goals.js`

- `innerHTML` writes: lines 31 (clear), 49 (renders goals — uses
  `escapeHtml(g.label)`).
- `eval` / `Function` constructor: none.
- `fetch()` URLs: `/api/first-week/goals` (81), `/api/first-week/widget/dismiss`
  (65, POST with `x-csrf-token`). Both are literal paths. No SSRF.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding FW1 — `escapeHtml` is robust (Info)

`escapeHtml()` (16) handles `&`, `<`, `>`, `"`, `'` and is the only path for
goal labels to enter the DOM. No issues.

### Finding FW2 — CSRF token read from `_csrf` cookie via regex (Low)

`csrf()` (22) reads `document.cookie` with a regex. Cookie injection from
another tab is not in narve's threat model (HttpOnly is not set on `_csrf`,
which is required for the double-submit pattern). Noted as Low / by design.

**Severity: 0 Critical, 0 High, 0 Medium, 1 Low.**

---

## `onboarding_tour.js`

- `innerHTML` writes: line 117 — the spotlight + popover. Variables
  interpolated are: `idx` (integer), `total` (integer), `spotlightTop`
  / `Left` / `W` / `H` / `popoverTop` / `popoverLeft` (numbers derived
  from `getBoundingClientRect()`), and `escapeHtml(step.title)` /
  `escapeHtml(step.body)`. `STEPS` is a static constant defined at the
  top of the file (22). No attacker reach.
- `eval` / `Function` constructor: none.
- `fetch()` URLs: `/api/onboarding/tour-state` (233),
  `/api/onboarding/tour-complete` (213), `/api/onboarding/tour-skip`
  (219). All literal. No SSRF.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding OB1 — `document.querySelector(selector)` from a static array (Info)

`findTarget()` (76) calls `querySelector` on `STEPS[i].target`. Inputs are
file-internal constants (`[data-tour='feed']` …). No tainted-selector risk.

**Findings:** none meaningful. **Severity: clean.**

---

## `realtime-bindings.js`

- `innerHTML`/`outerHTML` writes: none.
- `eval` / `Function` constructor: none.
- `fetch()`: none.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

Reads from `body.dataset` and dispatches to `window[name]()`-style handlers
chosen from a closed switch on `envelope.type` (line 41). The
`call("handleRtEvent", envelope)` default-branch (63) is a controlled lookup
of a function name fixed at compile time, NOT a string from the envelope —
so an envelope can't pick its own JS handler.

### Finding RB1 — `body.dataset.realtimeMarket` flows into subscribe channel name (Low)

Lines 69–83 take `data-realtime-*` attributes and concatenate them into
channel names (`"market:" + marketSlug`, `"subproduct:" + subproductSlug`).
The channel string is sent to the server over WebSocket (`{ op: "subscribe",
channel }`); the server is responsible for ACL on channel subscription. If
an attacker can set `data-realtime-subproduct` on the body (template
injection elsewhere → that's not a JS-layer bug), they could request any
channel — server-side authz is the gate. Note for the WebSocket subscribe
handler, not for this file.

### Finding RB2 — `userId` regex-checked but `marketSlug`/`subproductSlug` are not (Low)

Line 76 enforces `/^\d+$/` on `userId`. The market/subproduct slugs are not
validated. Per RB1 this is server-side authz's responsibility, but a small
client-side allowlist (`/^[a-z0-9:_\-]+$/i`) would harden the
defence-in-depth posture.

**Severity: 0 Critical, 0 High, 0 Medium, 2 Low.**

---

## `realtime.js`

- `innerHTML`/`outerHTML` writes: none.
- `eval` / `Function` constructor: none.
- `fetch()`: none (uses `WebSocket`).
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding RT1 — WebSocket URL trusts `location.host` and protocol upgrade (Info)

`connect()` (40) builds `${proto}//${location.host}/ws`. `location.host` is
the current page's host — same-origin by construction. No SSRF surface.

### Finding RT2 — `JSON.parse(event.data)` in message handler (Info)

Line 66 parses every WebSocket frame as JSON inside a try/catch. Safe.
`envelope` is later only dispatched to listeners as a plain object — no
HTML, no script evaluation, no DOM injection at this layer.

### Finding RT3 — listener exceptions swallowed in `_dispatch` (Low)

Lines 148, 155: try/catch around `fn(envelope)` swallows all errors. Defensive
choice (one buggy listener can't kill the bus). No security implication, but
makes downstream listener bugs invisible — call out for ops, not for this
audit.

**Severity: 0 Critical, 0 High, 0 Medium, 1 Low (operational).**

---

## `share-button.js`

- `innerHTML`/`outerHTML` writes: none — labels are written via
  `btn.textContent` (line 93).
- `eval` / `Function` constructor: none.
- `fetch()` URLs: `ENDPOINT_BY_KIND[kind]` (34, 131). `kind` comes from
  `btn.dataset.shareKind` (114). The dataset value is keyed into a static
  map; an unknown kind returns `undefined` and the function exits at 115.
  No arbitrary URL fetch.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding SB1 — `data.share_url` flows into `window.location.href` (Medium)

Line 149: on 402 the page navigates to `/subscribe` (static, OK).
Line 183: when clipboard write fails, the URL is shown inline via
`setLabel(btn, data.share_url, "share-btn-copied")` — that uses
`btn.textContent`, safe.
Line 178/109: `copyToClipboard(fullShareUrl(data.share_url))` —
`fullShareUrl()` returns `window.location.origin + pathPart`. If the server
returns a `share_url` containing `://` (e.g. `https://evil.example/x`), the
client treats it as a relative path and prepends the origin, so the
clipboard content stays on-origin. But — `fullShareUrl()` does NOT validate
that `pathPart` starts with `/`. A server response of `share_url:
"//evil.example/x"` becomes `https://narve.aievil.example/x` after
concatenation; mostly broken, but a `share_url` of `javascript:alert(1)//`
becomes `https://narve.aijavascript:alert(1)//` — likewise broken. **Not
exploitable as-is** because nothing in this file navigates to the clipboard
contents; it's pasted by the user into their own client.

The `share_url` is never assigned to `window.location.href` directly. Clean
on the navigation axis. **Medium → downgraded to Info** after re-verifying
the flow.

### Finding SB2 — `btn.textContent = data.share_url` on copy failure (Low)

Line 183: when clipboard fails, the literal `share_url` is written as the
button label via `setLabel` → `btn.textContent`. `textContent` is XSS-safe.
Worst case: an evil `share_url` is displayed verbatim. No execution.

**Severity: 0 Critical, 0 High, 0 Medium, 1 Low.**

---

## `share_menu.js`

- `innerHTML` writes: lines 162 (trigger button — only static SVG + label
  strings), 177 (menu body — interpolates `escAttr(xUrl)` and
  `escAttr(opts.ogUrl)`).
- `eval` / `Function` constructor: none.
- `fetch()`: none.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding SM1 — `opts.url` and `opts.ogUrl` come from `data-share-*` attributes (Low/Medium)

Lines 290–293: `opts` is built from `el.dataset.shareUrl`,
`shareTitle`, `shareMarkdown`, `shareOg`. These flow to:
- `xUrl` (172) → `https://x.com/intent/tweet?...&url=${encodeURIComponent(opts.url)}`
  — encoded, then `escAttr(xUrl)` inserted into `href="…"`. Two layers of
  defense; clean.
- `opts.ogUrl` → `escAttr(opts.ogUrl)` into `href="…"` (189). `escAttr`
  encodes `<>"'&` but NOT colons; an `og` of `javascript:alert(1)` would
  survive into the `href`. **This is the classic anchor-JS-URI vector.** The
  anchor has `target="_blank" rel="noopener noreferrer"` (190) but those
  don't block `javascript:` execution.

Reach: `data-share-og` is set by server templates on cards (e.g.
`/og/market/foo`). If template rendering ever passes a user-controlled
string into `data-share-og` without an allowlist, this is **High**. Today
it's controlled server-side, so practical severity is **Medium** —
defence-in-depth gap, no current exploit.

### Finding SM2 — `opts.title` set on `trigger.setAttribute("aria-label", …)` (Info)

Line 161: `setAttribute` is safe for attribute values; not an HTML-parser
context.

### Finding SM3 — `copyToClipboard` uses execCommand fallback (Low)

`execCommand("copy")` is deprecated but functions in current browsers; not
a security issue, just future fragility.

**Severity: 0 Critical, 0 High, 1 Medium, 2 Low.**

---

## `shortcuts-discovery.js`

- `innerHTML` writes: line 50 — the hint toast body. Content is a static
  template string with literal `<kbd>?</kbd>` markup. No interpolation.
- `eval` / `Function` constructor: none.
- `fetch()`: none.
- `localStorage`: read+write of `narve.shortcutHintDismissed` (boolean flag,
  no credential).
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

**Findings:** none. **Severity: clean.**

---

## `toast.js`

- `innerHTML` writes: none — message body is written via `msg.textContent =
  message` (69), action label via `btn.textContent = action.label` (85).
- `eval` / `Function` constructor: none.
- `fetch()`: none.
- `localStorage`: none.
- `postMessage`: none.
- `document.write`: none.
- jQuery `.html()`: jQuery not used.

### Finding T1 — `action.onClick` callback executed synchronously (Info)

Line 88: `action.onClick && action.onClick()`. Callers supply this. Not an
attack surface unless an attacker can register a toast (call site enforces
trust). Clean.

**Findings:** none meaningful. **Severity: clean.**

---

## Aggregate severity

| Severity | Count |
| -------- | ----- |
| Critical | 0     |
| High     | 0     |
| Medium   | 4     |
| Low      | 11    |
| Info     | 6     |

Clean files (zero findings): `admin-shell.js`, `onboarding_tour.js`,
`shortcuts-discovery.js`, `toast.js`.

## Top 5 issues to address

1. **CP2 — `command-palette.js` `_html` server contract is implicit.**
   `renderGroups()` interpolates `item.title_html` / `item.subtitle_html`
   into `innerHTML` after `renderHighlight()`. The two-layer
   escape→re-emit-`<mark>` is safe today only because the server's
   `_html` payload is narrow. Lock the contract — either rename the field
   to `_marked_snippet` and reject any non-`<mark>` content in the
   client, or have the server send a structured `{ before, match, after
   }` triple and stop trusting an HTML string at all.

2. **CP1 / C1 — duplicate `<mark>` re-emit logic in cmdk.js and
   command-palette.js.** Same fragility, two implementations. Consolidate
   into one shared helper with a unit test covering ``<mark>foo</mark>``,
   ``<mark>`<img onerror>`</mark>``, and ``foo <mark x="bar">y</mark>``
   (the last must reject the attribute).

3. **SM1 — `share_menu.js` `data-share-og` allows `javascript:` URIs.**
   `escAttr()` does not strip dangerous URI schemes from the value
   inserted into `<a href="…">`. Add a `safeUrl(s)` allowlist that
   accepts only `http(s):` and `/`-relative URLs, applied to every
   anchor `href` built from a `data-share-*` attribute.

4. **RB2 — `realtime-bindings.js` market / subproduct slug client-side
   allowlist absent.** `body.dataset.realtimeMarket` and
   `realtimeSubproduct` concatenate into channel names without
   validation. Server-side ACL is the real gate, but a
   `/^[a-z0-9:_\-]{1,80}$/i` check here would short-circuit malformed
   slugs and reduce blast radius if a template ever leaks user input
   into the body dataset.

5. **C3 / FW2 — localStorage / cookie reads are unauthenticated.**
   `cmdk.js` reads `localStorage["nv-cmdk-recent"]` and trusts the JSON;
   `first_week_goals.js` reads the `_csrf` cookie via regex. Neither is a
   credential surface, but documenting both in a SECURITY notes section
   (along with the deliberate double-submit cookie design) avoids future
   churn when an auditor flags the same patterns again.

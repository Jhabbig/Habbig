# i18n audit — locale data integrity, language-code allowlist, fallback chain

**Date:** 2026-05-15
**Scope:** narve.ai gateway i18n subsystem — locale JSON files, translator, detector, server integration, client-side mirror.
**Method:** synchronous bash only; static read + JSON parse + regex scan. No live probes, no edits. Pre-release surfaces (`/`, `/gate`, `prerelease.html`) treated as off-limits — none of the i18n module is rendered there today, and no prerelease assets were touched.

---

## Files inventoried

### Server-side i18n module
- `gateway/i18n/__init__.py` — public API exports
- `gateway/i18n/translator.py` — `t(key, lang, **kwargs)`, `load_locale`, `_resolve` (machine-flag unwrapper)
- `gateway/i18n/detector.py` — `detect_language`, `normalise_lang`, `parse_accept_language`
- `gateway/i18n/format.py` — `format_number / _currency / _percent / _date`
- `gateway/i18n/auto_translate.py` — one-off Claude-Sonnet translation pipeline (CLI; not in request path)

### Locale data
- `gateway/i18n/locales/en.json` — 262 keys, plain strings (source of truth)
- `gateway/i18n/locales/es.json` — 262 keys (257 `_machine`, 5 human-reviewed)
- `gateway/i18n/locales/de.json` — 262 keys (255 `_machine`, 7 human-reviewed)
- `gateway/i18n/locales/pt-br.json` — 262 keys (257 `_machine`, 5 human-reviewed)
- `gateway/i18n/locales/candidates.json` — 197 keys, extractor output (developer-only, never loaded by the runtime)

### Integration & support
- `gateway/server.py` (lines ~2496-2704) — render-time `{{ t(...) }}` substitution + `<script type="application/json" id="__NARVE_I18N__">` locale blob inlining
- `gateway/server_features.py` (lines ~148-223) — `/api/set-language` route, validates against `SUPPORTED`
- `gateway/static/i18n-client.js` — browser-side `window.t()` reading the inlined blob
- `gateway/migrations/125_preferred_language.py` — adds `users.preferred_language TEXT DEFAULT 'en'`
- `gateway/scripts/extract_strings.py` — emits `candidates.json` (offline tool)
- `gateway/tests/test_i18n.py` — translator + detector + set-language coverage

---

## Severity counts

| Severity | Count |
|---|---|
| Critical | 0 |
| High     | 0 |
| Medium   | 2 |
| Low      | 3 |
| Info     | 3 |

---

## Findings

### M-1 (MEDIUM) — `load_locale(lang)` is exported without an allowlist guard
**File:** `gateway/i18n/translator.py:36-56`, `gateway/i18n/__init__.py:17,36`
**Detail:** `_locale_path(lang)` does `LOCALES_DIR / f"{lang}.json"`. There is no `normalise_lang()` check before the path join, no `.resolve().is_relative_to(LOCALES_DIR)` check, and `path.exists()` is the only gate. A caller passing an attacker-controlled raw value (e.g. `../../../../etc/hosts.allow`) would have `Path` happily compose a traversal segment; `path.exists()` would then test the absolute target. Even though `json.loads` over a non-JSON file would log-and-return `{}`, the attempted read still constitutes path traversal + an info leak via the `OSError`/`JSONDecodeError` branch (logged at WARN).
**Today's blast radius:** none in production — both real callers (`server.py:2688` via `detect_language`, and `t()` itself which is fed by the same detector) only ever pass values that round-tripped through `normalise_lang()` and `SUPPORTED`. The exposure is latent: `load_locale` is in `__all__`, so a future caller (admin tool, CLI script, route handler) could pass raw input.
**Fix:** add an early `if lang not in SUPPORTED: return {}` at the top of `load_locale`, OR have `_locale_path` resolve and assert containment in `LOCALES_DIR`.

### M-2 (MEDIUM) — `_machine` entries are silently fallback-elided when the wrapper is malformed
**File:** `gateway/i18n/translator.py:66-81`
**Detail:** `_resolve()` returns `None` for any dict that lacks a string `"text"` key. The fallback chain then jumps to English. That's the right behaviour *except* it is silent — there is no log, no metric, no test asserting the count. If a future `auto_translate` run accidentally writes `{"text": null, "_machine": true}` or `{"text": ["a","b"], "_machine": true}` for a swath of keys, every affected user-facing string in that locale will silently revert to English with zero operator signal. Combined with `_cache` never being invalidated outside `clear_cache()`, the regression survives container restarts only because each fresh worker re-reads the bad file and gets the same bad data.
**Fix:** in `_resolve`, log at `log.debug` when an entry is a dict but `text` is non-string; expose a counter (or add a startup-time integrity check that asserts every value in every shipped locale resolves to a non-empty string). Tests should add a case that writes a malformed `_machine` entry and asserts both fallback **and** a log emission.

### L-1 (LOW) — locale blob inlining uses minimal `</` escape only
**File:** `gateway/server.py:2689-2696`
**Detail:** `_locale_json_safe = _locale_json.replace("</", "<\\/")` is sufficient per HTML5 — inside `<script type="application/json">`, the parser only terminates on `</script` (or `</SCRIPT` etc.), so escaping every `</` is safe and complete. Locale values are scanned (this audit) and contain no `<` characters, no event handlers, no `javascript:` schemes, no `<script>`, no `${` template-literals, no proto-pollution keywords. Risk is therefore latent — any future translator that introduces HTML into a value would still be safe inside the JSON island, but `window.t()` callers that insert into `innerHTML` (vs `textContent`) would expand the surface. The client doc string at `i18n-client.js:19-21` already notes "translator doesn't escape. Caller is responsible." — sound, but worth a lint/grep rule.
**Fix:** add a unit test that locale values never match `/<[a-z]/i`; document in `I18N_HANDOFF.md` that locale values must remain pure text.

### L-2 (LOW) — `detect_language` reads `request.state.user["preferred_language"]` without re-normalising at storage time
**File:** `gateway/i18n/detector.py:104-111`, `gateway/server_features.py:184-197`
**Detail:** `/api/set-language` normalises before writing to `users.preferred_language`, so the DB value is always a SUPPORTED tag. But the detector re-normalises on read anyway (defensive), so a manually-edited DB row with `preferred_language = '../foo'` would still fall through to the next source. Good defence in depth, but it would be worth a CHECK constraint on the column (`CHECK (preferred_language IN ('en','es','de','pt-br') OR preferred_language IS NULL)`) — migration 125 sets `DEFAULT 'en'` but does not constrain values.
**Fix:** future migration adds a CHECK; meanwhile the detector's safety net is fine.

### L-3 (LOW) — `parse_accept_language` accepts unbounded input and `q` values outside `[0..1]`
**File:** `gateway/i18n/detector.py:48-75`
**Detail:** No max length cap on the header (Starlette already caps request headers, so this is theoretical). Negative or >1 `q` values are accepted as-is and sort accordingly — RFC 7231 says `q` is `0..1` to 3 decimal places. Worst case: a client sends `q=999` to bias a tag; since the resulting tag still has to pass `normalise_lang()` against `SUPPORTED`, this is harmless. Worth clamping to `[0.0, 1.0]` for hygiene.
**Fix:** clamp `q = max(0.0, min(1.0, q))` in `parse_accept_language`.

### I-1 (INFO) — `candidates.json` contains HTML fragments (developer file only)
**File:** `gateway/i18n/locales/candidates.json:107,118` (e.g. `server.a_href_gate_have_an_invite_token_use_it = "<a href=\"/gate\">…</a>"`)
**Detail:** `candidates.json` is written by `gateway/scripts/extract_strings.py` and is never loaded by the runtime — `LOCALES_DIR` is iterated by name (`en.json` / `es.json` / `de.json` / `pt-br.json`) inside `_locale_path(lang)`, and no code path opens `candidates.json`. The two HTML fragments listed there are extractor noise from `gate.html` / auth pages and should be hand-cleaned before promotion to `en.json`. Not a runtime risk — flagged only so a future engineer doesn't promote raw HTML into a live locale.

### I-2 (INFO) — JSON parse confirms structural integrity
All five files parse as valid UTF-8 JSON. All three target locales (`es`, `de`, `pt-br`) have exactly the same 262 keys as `en.json` — 0 missing, 0 extra. All placeholder tokens (`{count}`, `{remaining}`, `{total}`, etc.) are preserved byte-for-byte from `en.json` into the three targets — 0 placeholder mismatches across 786 cross-locale checks. No string in any locale exceeds 500 chars; the longest is well under one screen.

### I-3 (INFO) — Locale data injection patterns
Adversarial regex sweep of all 4 active locale files (1,048 values) for: `<script`, `</script>`, `javascript:`, event handlers (`on\w+=`), `<iframe`, `<img onerror`, hex escapes, `__proto__` / `constructor` / `prototype`, template literal `${`, `eval`. **Zero hits.** Locale data is, today, plain UI text with no executable surface.

---

## Fallback chain — verified

Trace per `gateway/i18n/translator.py:84-111`:

1. `lang = (lang or DEFAULT).lower()` — None / empty / falsy → `"en"`.
2. `primary = load_locale(lang)` — missing file or malformed JSON → `{}` (logged at WARN), no exception bubbles.
3. `resolved = _resolve(primary.get(key))` — plain string OR unwrapped `{text, _machine}` dict OR `None`.
4. If `resolved is None` and `lang != DEFAULT`, retry against `load_locale(DEFAULT)`.
5. If still `None`, `resolved = key` — guarantees a non-None return value.
6. `if kwargs: resolved.format(**kwargs)` — wrapped in try/except over `KeyError, IndexError, ValueError`; on failure returns the raw template (the comment says "better to show a {name} than to 500 the page").

This is the correct chain (requested → DEFAULT → raw key) and is exercised by `tests/test_i18n.py::test_fallback_to_english`, `test_fallback_missing_es_but_present_in_en`, and the malformed-JSON cases at lines 228/238. **Pass.**

---

## Language-code allowlist — verified

- `SUPPORTED = ["en", "es", "de", "pt-br"]` (`translator.py:30`) is the single source of truth.
- `normalise_lang()` (`detector.py:29-45`) lowercases, swaps `_` → `-`, then matches against `SUPPORTED` directly OR via the primary subtag (`pt` → `pt-br`). Returns `None` for everything else.
- `/api/set-language` (`server_features.py:183-188`) rejects anything `normalise_lang` doesn't recognise with HTTP 400 + a `{"error": "unsupported_language", "supported": [...]}` body.
- `detect_language` (`detector.py:86-133`) funnels every input source — query, user pref, cookie, `Accept-Language` — through `_first_supported(...) → normalise_lang(...)`. Any unsupported value falls through silently to the next source, never raises.
- `<html lang="...">` injection in `server.py:2664-2669` HTML-escapes `_lang` before substitution, so even if the allowlist were bypassed upstream, the rendered attribute would be safe.

**The only allowlist gap is L-1 / M-1: `load_locale(lang)` does not enforce SUPPORTED at the function boundary.** All in-tree callers are clean today; the gap is a guard against future misuse.

---

## Locale data integrity — verified

- All 4 active locale files are well-formed JSON (UTF-8).
- Key parity: 262/262 across en/es/de/pt-br. No missing, no extra.
- Placeholder integrity: every `{var}` token in `en.json` is present, identically, in every target locale (786 / 786 checks pass).
- Value-shape integrity: every value is either a plain string OR `{"text": <str>, "_machine": true}`. No nulls, no arrays, no nested objects beyond the documented machine wrapper.
- No executable-injection patterns (script tags, JS schemes, event handlers, template literals, `eval`, proto-pollution, hex escapes) in any of the 1,048 string values across the active locales.
- Two odd-shaped keys (`feed.X_predictions`, `feed.X_new`) use uppercase `X` as a placeholder convention — by name only, not a format-string placeholder. No collision with the `{name}`-style runtime substitution.

---

## Top 3 actions

1. **M-1 — Tighten `load_locale`.** Add `if lang not in SUPPORTED: return {}` at the top of `gateway/i18n/translator.py::load_locale`. One line, zero behaviour change for current callers, closes the latent traversal surface.
2. **M-2 — Add a startup-time locale integrity assertion.** Iterate every key in every shipped locale; for each, confirm `_resolve(...)` returns a non-empty string. Surface the count as a structured log line at boot (`i18n: en=262 es=262 de=262 pt-br=262, machine-flagged es=257 de=255 pt-br=257`) so silent regressions in `_resolve` show up in the next deploy log.
3. **L-1 — Lock down locale-value shape with a test.** Add `tests/test_i18n.py::test_no_html_in_locale_values` that asserts no value matches `/<[a-z]/i`. Cheap insurance against a future auto-translate run that produces HTML, and a clear contract for human reviewers.

---

## Out of scope / not touched

- Pre-release surfaces: `/`, `/gate`, `prerelease.html`, `static/prerelease.html`. None render `{{ t(...) }}` today (verified — only `landing.html` does). No edits proposed against prerelease HTML.
- Live HTTP probes, edge-rule changes, secrets rotation — none performed.
- `auto_translate.py` API key handling — outside i18n locale-data scope (it's a build-time CLI; covered separately under secrets handling).

---

*Audit method: synchronous bash, file reads, Python JSON parse + regex scan. No subprocess fan-out, no network calls.*

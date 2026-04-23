# i18n foundation — handoff

EN / ES / DE / PT-BR scaffolding for the narve.ai gateway. Static UI
strings translated; market data stays English (sourced that way).

---

## What ships in this session

### Migration 125 — `users.preferred_language`

[`gateway/migrations/125_preferred_language.py`](gateway/migrations/125_preferred_language.py)

Adds `preferred_language TEXT DEFAULT 'en'` to the users table. Safe to
replay: PRAGMA check before ALTER. Down migration rebuilds via the
SQLite column-rename dance. `down_revision = "116"` (current head).

### `gateway/i18n/` package

| File | What it does |
|------|--------------|
| [`__init__.py`](gateway/i18n/__init__.py) | Public exports: `t`, `detect_language`, `SUPPORTED`, `DEFAULT`, `LANG_COOKIE_NAME`, `normalise_lang`, `parse_accept_language`, `clear_cache`, `load_locale` |
| [`translator.py`](gateway/i18n/translator.py) | `t(key, lang, **kwargs)` with fallback chain: requested → `en` → raw key. Unwraps `{"text": "...", "_machine": true}` entries automatically. Never raises. |
| [`detector.py`](gateway/i18n/detector.py) | `detect_language(request)` with strict precedence: query `?lang=` → `request.state.user.preferred_language` → `lang` cookie → `Accept-Language` header → `"en"` |

### Locale files

~120 keys each. Naming: dotted semantic keys (`nav.billing`,
`empty.saved.title`, `error.paywall.body`).

| File | Shape | Notes |
|------|-------|-------|
| [`gateway/i18n/locales/en.json`](gateway/i18n/locales/en.json) | Plain strings | Source of truth. Human-authored. |
| [`gateway/i18n/locales/es.json`](gateway/i18n/locales/es.json) | `{"text": "...", "_machine": true}` entries | Hand-seeded during this session, flagged for review. |
| [`gateway/i18n/locales/de.json`](gateway/i18n/locales/de.json) | Same as es | Same caveat. |
| [`gateway/i18n/locales/pt-br.json`](gateway/i18n/locales/pt-br.json) | Same as es | Same caveat. |

Human-reviewed strings drop the wrapper and go back to plain strings, so
a grep for `"_machine": true` after translation QA instantly reports
outstanding review work.

### `render_page()` integration

`gateway/server.py` now:

1. Calls `detect_language(request)` once per render and exposes `lang`
   in the template context.
2. Substitutes `{{ t("key") }}` and `{{ t("key", count=3) }}` patterns
   in templates before the existing `{{ key }}` substitution pass. The
   substituted output is HTML-escaped.
3. Rewrites the `<html>` tag so `lang="<detected>"` is always present.
4. Injects `<script>window.LANG='<lang>'; window.SUPPORTED_LANGS=[...]</script>`
   right after `<body>` so client-side `Intl.*` code has the locale
   without a separate fetch.

Zero template changes are required for the detection to work — pages
just get the new `<html lang>` and `window.LANG` for free. Pages that
want translations convert strings to `{{ t("some.key") }}` lazily.

### `POST /api/set-language`

`gateway/server_features.py`. Accepts either `?lang=es` or
`{"lang": "es"}` JSON. Validates via `normalise_lang` (handles `pt_BR`,
`pt-br`, `PT-BR`, bare `pt` → `pt-br`). On success:

- Sets `lang` cookie, 180-day max-age, `HttpOnly=false` so the client
  switcher can read it without a round-trip.
- If the session is authenticated, persists
  `users.preferred_language = ?`. Missing-column failures are
  swallowed (best-effort) so the switch still works in the cookie-only
  path when migration 125 hasn't run.
- Returns `{"ok": true, "lang": "es", "persisted": <bool>}`.

### `scripts/extract_strings.py`

Walks `gateway/static/*.html` and every `render_page(...)` call site in
`gateway/*.py`, emits a `gateway/i18n/locales/candidates.json` keyed by
semantic dotted keys. Skips CSS/JSON noise, `{{ placeholder }}` blocks,
values under 2 chars. `--diff` prints only brand-new keys. **Not run
in this session** — the expected output is noisy and needs human
review before merging into `en.json`.

### `gateway/i18n/auto_translate.py`

One-off Sonnet-backed translator. Reads `en.json`, writes machine-flagged
entries to `es.json` / `de.json` / `pt-br.json` for every key where the
target is missing or still `_machine`-flagged. Never overwrites a plain
string (marks the key as human-reviewed). Pins brand names
(narve.ai / Polymarket / Kalshi / Intelligence) and `{placeholder}`
tokens in the system prompt. **Not run in this session** — requires
`ANTHROPIC_API_KEY`; the ~120-key starter set was hand-translated
instead so the package is immediately usable.

### Tests

[`gateway/tests/test_i18n.py`](gateway/tests/test_i18n.py) — 32 tests, all passing.

- `t()` direct hits, `_machine` unwrap, fallback to `en`, missing-key
  behaviour, placeholder substitution, missing-kwarg safety.
- `normalise_lang` covers `pt_BR`, `pt_br`, `PT-BR`, bare `pt`, unsupported.
- `parse_accept_language` handles weighted tags, malformed q, empty input.
- `detect_language` precedence: query > user > cookie > header > default.
- `/api/set-language` — 400 on unsupported, 200 on query + JSON paths,
  cookie set, `users.preferred_language` persisted for authed sessions,
  `pt_BR` normalisation round-trip.
- Malformed locale file doesn't crash — missing file → empty dict, bad
  JSON logs + returns empty dict.

---

## Language switcher UI — queued

Not shipped this session. The backend is ready (cookie + DB write + `t()`
in every render), but no UI widget yet. Stub recipe for next session:

```html
<!-- Drop-in topbar fragment -->
<div class="lang-switcher">
  <button onclick="showLangMenu(event)">
    <span data-lang-flag>🇺🇸</span>
    <span data-lang-name>English</span>
  </button>
  <ul class="lang-menu" hidden>
    <li><a data-lang="en">🇺🇸 English</a></li>
    <li><a data-lang="es">🇪🇸 Español</a></li>
    <li><a data-lang="de">🇩🇪 Deutsch</a></li>
    <li><a data-lang="pt-br">🇧🇷 Português (BR)</a></li>
  </ul>
</div>
<script>
document.querySelectorAll('[data-lang]').forEach(a => {
  a.addEventListener('click', async (e) => {
    e.preventDefault();
    const lang = a.dataset.lang;
    await fetch('/api/set-language', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'x-csrf-token': getCsrf()},
      credentials: 'same-origin',
      body: JSON.stringify({lang}),
    });
    location.reload();
  });
});
</script>
```

Best place to put it: the sidebar footer, next to the theme toggle.
Full-page reload is the correct move — it lets `render_page()` re-run
with the new locale on every template.

---

## What's queued for follow-up sessions

1. **Landing the switcher UI** (~30 min) — partial above + CSS.
2. **Run `extract_strings.py`** and merge the curated candidates into
   `en.json` to grow the corpus toward the 500-key target.
3. **Run `auto_translate.py`** to populate ES / DE / PT-BR for every
   new `en` key. Budget: Sonnet can do ~30 keys per call × ~15 batches
   ≈ $2 of API usage.
4. **Template conversion** — walk the highest-traffic pages (landing,
   dashboard, billing) and replace hardcoded strings with `{{ t(...) }}`
   calls. Low-traffic admin pages can stay English for now.
5. **Email templates** — `gateway/email_system/templates/*.html`
   currently hardcode English. They need the same `t()` substitution
   pass. ~130 strings across 10 templates per the original brief.
6. **Pluralisation** — current shape `{{ t("feed.X_predictions",
   count=n, plural="" if n == 1 else "s") }}` is English-biased.
   Spanish / German / Portuguese need dedicated plural rules. Flag as
   a separate session after ICU / Fluent evaluation.
7. **Client-side `t()`** — window.LANG is exposed but there's no
   window.t yet. For feed row rendering or toast messages that
   originate in JS, we either (a) ship a tiny `gateway/static/i18n.js`
   that fetches `/static/i18n/<lang>.json` and does the same lookup
   client-side, or (b) render the template strings server-side into
   `data-*` attrs and read them in JS. (b) is simpler.
8. **axe-core / RTL testing** — not needed yet (all four languages are
   LTR) but worth a note before Arabic / Hebrew get added.

---

## Cold-start guide — running it

```bash
# Apply the migration (existing pattern)
python3 -c 'import migrations; migrations.upgrade_to_head()'

# Run tests
python3 -m pytest gateway/tests/test_i18n.py -q

# Scan the codebase for new translatable strings
python3 gateway/scripts/extract_strings.py --diff

# Auto-translate new keys (requires ANTHROPIC_API_KEY)
python3 -m gateway.i18n.auto_translate --dry-run
python3 -m gateway.i18n.auto_translate --langs es
```

---

## Files touched this session

```
gateway/migrations/125_preferred_language.py   NEW
gateway/i18n/__init__.py                       NEW
gateway/i18n/translator.py                     NEW
gateway/i18n/detector.py                       NEW
gateway/i18n/auto_translate.py                 NEW
gateway/i18n/locales/en.json                   NEW (~120 keys)
gateway/i18n/locales/es.json                   NEW (machine-flagged)
gateway/i18n/locales/de.json                   NEW (machine-flagged)
gateway/i18n/locales/pt-br.json                NEW (machine-flagged)
gateway/scripts/extract_strings.py             NEW
gateway/server.py                              MODIFIED (render_page hook)
gateway/server_features.py                     MODIFIED (/api/set-language)
gateway/tests/test_i18n.py                     NEW (32 tests)
I18N_HANDOFF.md                                NEW (this file)
```

Not deployed this session — a follow-up task on branch
`feature/platform-build` should review the switcher UI + extractor run
before pushing.

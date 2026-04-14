# midterm-dashboard/frontend/src/lib/ — Shared client helpers

Cross-cutting utilities used by multiple pages. Currently the API client and
the global settings context (unit system, theme, etc.).

## Files in this directory

| File | Purpose |
|---|---|
| `api.js` | Thin `fetch` wrapper exporting an `api` object. Sends `credentials: 'include'` so cookies are forwarded, dispatches an `auth:unauthorized` event on 401 (caught in `App.jsx` to clear the user state), and JSON-decodes the body. **All HTTP calls in the frontend should go through this.** Includes the admin human-review helpers `flagMarket(raceKey, source, sourceId, note?)`, `unflagMarket(raceKey, source, sourceId)`, `verifyRace(raceKey, note?)`, and `unverifyRace(raceKey)` — all use URL-encoded path params and hit the `/admin/race/{key}/flag` and `/admin/race/{key}/verify` endpoints. |
| `settings.jsx` | `SettingsProvider` React context + `useSettings()` hook. Stores per-user preferences (unit system: American $/en-US vs European €/de-DE, theme, etc.) in `localStorage` under `midterm_settings`. Also exports module-level helpers (`_getUnitSystem`, etc.) so non-React code can read settings without a hook. |

> **Note:** the entire `src/lib/` directory used to be silently swallowed
> by a Python `lib/` rule in the repo-root `.gitignore`. The root gitignore
> now has an explicit unignore for this path — if you add new files here,
> double-check `git status` actually shows them.

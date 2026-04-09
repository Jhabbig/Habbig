# midterm-dashboard/frontend/src/ — React source

The React app entry point and route shell. Auth context, settings provider,
top nav, and the route table that maps each URL to a `pages/*` component.

## Files in this directory

| File | Purpose |
|---|---|
| `main.jsx` | Vite entry point — calls `createRoot` and mounts `<App />` inside `#root`. |
| `App.jsx` | Top-level component. Wraps the app in `<AuthProvider>` + `<SettingsProvider>`, defines all the routes (`/`, `/races`, `/race/:id`, `/divergence`, `/historical`, `/world`, `/admin`, `/account`, `/settings`, `/login`, `/register`), and renders the side-nav. |
| `index.css` | Global stylesheet — Tailwind directives, base resets, custom CSS variables. |

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `pages/` | One file per route (Dashboard, Races, RaceDetail, Divergence, Historical, ...) | `pages/README.md` |
| `lib/` | Shared client helpers (`api.js`, `settings.jsx`) | `lib/README.md` |
| `components/` | Reusable presentational components — empty placeholder, to be populated as the UI grows. | — |
| `hooks/` | Custom React hooks — empty placeholder. | — |
| `assets/` | Imported static assets (images, SVGs) — empty placeholder. | — |

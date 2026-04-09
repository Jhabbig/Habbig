# midterm-dashboard/frontend/ — React + Vite + Tailwind UI

The browser-side half of the Midterm Dashboard. Vanilla React 18 (no
Next.js / Remix / etc.) bundled with Vite, styled with Tailwind, charts with
Recharts, icons from lucide-react.

In production the build output (`dist/`) is served as static files by the
FastAPI backend on port 8051. In dev, run Vite separately on port 3000 and
let it proxy `/data/*` and `/auth/*` to the backend.

## Run locally

```bash
npm install
npm run dev    # Vite dev server on http://localhost:3000
npm run build  # writes ./dist (consumed by the FastAPI backend in production)
```

## Files in this directory

| File | Purpose |
|---|---|
| `package.json` | npm manifest. Scripts: `dev`, `build`, `preview`. Deps: react, react-dom, react-router-dom, recharts, lucide-react. Dev deps: vite, tailwindcss, postcss, autoprefixer, @vitejs/plugin-react. |
| `package-lock.json` | npm lockfile — commit alongside `package.json` so installs are reproducible. |
| `vite.config.js` | Vite config — React plugin, dev-server proxy rules to the FastAPI backend. |
| `tailwind.config.js` | Tailwind config — content globs into `src/**/*.{js,jsx}`, theme extensions. |
| `postcss.config.js` | PostCSS — tailwindcss + autoprefixer. |
| `index.html` | Vite entry HTML — mounts the React app at `#root`. |

## Subdirectories

| Dir | Purpose | README |
|---|---|---|
| `src/` | React source (App + pages + lib helpers) | `src/README.md` |
| `public/` | Static files copied as-is into `dist/` (favicons, etc.) — currently empty. | — |

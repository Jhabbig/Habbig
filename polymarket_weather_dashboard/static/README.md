# polymarket_weather_dashboard/static/ — Static assets (PWA)

Browser-side assets for the Polymarket Weather dashboard. The Flask
backend (`server.py`) serves this directory as static files. The
dashboard is set up as an installable Progressive Web App, so this
directory contains both the page templates **and** the PWA wiring
(manifest, service worker, app icons).

## HTML pages

| File | Purpose |
|---|---|
| `index.html` | The main weather-markets dashboard. Charts current Polymarket weather contracts against forecasts and observed conditions. |
| `admin.html` | Admin-only configuration / debug view — refresh data, edit watched markets, inspect raw API responses. |

## PWA + JS

| File | Purpose |
|---|---|
| `manifest.json` | Web App Manifest — name, short name, theme color, icon refs, `start_url`. Lets users install the dashboard to their home screen. |
| `sw.js` | Service worker — caches the shell HTML/CSS/JS for offline-first loads, intercepts API calls when offline. |
| `chart.umd.min.js` | Vendored Chart.js (UMD build) — the only chart library used. Vendored rather than CDN-loaded so the PWA works offline. |

## Icons

| File | Purpose |
|---|---|
| `icon.svg` | Source vector icon. |
| `icon.svg.png` | PNG export of the source SVG (kept for tools that don't render the SVG). |
| `icon-192.png` | 192×192 PWA icon (referenced from `manifest.json`). |
| `icon-512.png` | 512×512 PWA icon — used for splash screens and high-DPI installs. |

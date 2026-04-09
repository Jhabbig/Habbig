# world-state-dashboard/static/ — Vendored frontend libraries

Third-party JS/CSS dependencies served alongside `index.html`. Vendored
into the repo (rather than loaded from a CDN) so the dashboard renders
correctly even when the user is on a slow / restricted network and so
the build is fully reproducible.

## Files in this directory

| File | Purpose |
|---|---|
| `chart.umd.min.js` | Chart.js (UMD build) — used for the time-series charts in the world-state panels. |
| `maplibre-gl.js` | MapLibre GL JS — open-source vector-tile map renderer. Powers the world map view. |
| `maplibre-gl.css` | MapLibre GL stylesheet — required alongside the JS for popups, controls, and attribution to render correctly. |

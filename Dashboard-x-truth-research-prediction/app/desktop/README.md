# app/desktop/ — macOS desktop wrapper

PyInstaller bundles the FastAPI dashboard into a `.app`. This package wires up
a menu-bar control (rumps) and a native webview window (pywebview) so the
whole thing feels like a desktop app instead of a browser tab.

The bundled app starts the FastAPI server on a random localhost port, opens a
webview pointed at it, and adds a menu-bar icon for "Open Dashboard" /
"Refresh Now" / "Quit". When `sys.frozen` is set, working data is written
under `~/Library/Application Support/PolymarketDashboard/` instead of next to
the bundle.

Build the DMG with `scripts/build_dmg.sh` (uses `polymarket.spec`).

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `app_entry.py` | PyInstaller entry point. Detects frozen vs dev mode, sets a writable working dir, generates an SSO secret if missing, starts uvicorn in a thread, then hands off to the menu bar. |
| `menu_bar.py` | `PolymarketMenuBar` — rumps `App` subclass. Menu items: Open Dashboard, Refresh Now, Status, Quit. The Refresh button hits the FastAPI `/refresh` endpoint and posts an OS notification when the pipeline finishes. |
| `webview_window.py` | Thin wrapper around `pywebview.create_window` that opens the dashboard in a 1400×900 native window with the cocoa GUI backend. |

## Subdirectories

| Dir | Purpose |
|---|---|
| `assets/` | Icon files bundled into the `.app`. See `assets/README.md`. |

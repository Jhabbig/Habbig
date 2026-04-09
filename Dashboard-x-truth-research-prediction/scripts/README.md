# scripts/ — Build / release helpers

Helpers for packaging the dashboard as a macOS desktop app. Not used by the
Docker build or the regular dev workflow.

Build the DMG:

```bash
bash scripts/build_dmg.sh
# Output: PolymarketDashboard-1.0.0-arm64.dmg
```

Requires macOS 13+, Apple Silicon, `brew install create-dmg`, and Python 3.12.

## Files in this directory

| File | Purpose |
|---|---|
| `build_dmg.sh` | One-shot build pipeline. Cleans `dist/`, installs deps, runs `generate_icons.py`, runs `pyinstaller polymarket.spec`, builds the staged DMG with `create-dmg` and the background image below. |
| `generate_icons.py` | Generates `app/desktop/assets/icon.{icns,png}` and `menubar_icon.png` from scratch using Pillow. Run before bundling. |
| `dmg_background.png` | Background image for the DMG installer window. Referenced by `create-dmg` in `build_dmg.sh`. |

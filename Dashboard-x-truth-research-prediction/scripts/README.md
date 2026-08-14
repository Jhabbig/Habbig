# scripts/ — Build / release helpers + engine ops tooling

Helpers for packaging the dashboard as a macOS desktop app, plus the
operational tooling for the Stage-3 Prediction Engine (`app/engine/`). Not
used by the Docker build or the regular dev workflow.

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
| `load_test_engine.py` | Prediction Engine load test — N concurrent users against the fusion layer (in-process or `--url` HTTP mode), with a p95 latency assertion. `python scripts/load_test_engine.py --concurrency 500 --jobs 5000`. |
| `cost_readout.py` | One-page engine cost readout from the fusion_audit table: measured tokens/job, cache hit rate, $/1k predictions, tier mix, monthly projection. |
| `measure_tokens.py` | "The one number": runs a single live metric-prediction job and prints measured tokens in/out + priced cost (interactive / batch / warm-cache). Run this before scaling. Needs `ANTHROPIC_API_KEY`. |

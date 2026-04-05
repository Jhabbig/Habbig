# Polymarket Prediction Intelligence Dashboard

Scrapes X (Twitter) and TruthSocial for prediction-related posts, cross-references against live Polymarket odds, scores by EV and source credibility, surfaces the best betting opportunities.

## Quick Start

```bash
cd Dashboard-x-truth-research-prediction
cp .env.example .env   # edit with your credentials
docker-compose up --build
# Open http://127.0.0.1:18789
# Login: admin / changeme
```

## Without Docker

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 18789
```

## macOS Desktop App

```bash
pip install -r requirements.txt
bash scripts/build_dmg.sh
# Output: PolymarketDashboard-1.0.0-arm64.dmg
```

Requires: macOS 13+, Apple Silicon, `brew install create-dmg`

## Tests

```bash
pytest app/tests/ -v
```

## Configuration

Edit `app/config.yaml` for keywords, credibility weights, risk thresholds, quota limits.
Edit `.env` for API credentials and auth.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Auth required" JSON | Clear cookies, go to /login |
| No predictions | Add API creds to .env, click Refresh |
| Markets not matching | Lower market_match_threshold in config.yaml |
| X quota exhausted | Resets 1st of month |
| "App is damaged" (macOS) | Right-click -> Open |

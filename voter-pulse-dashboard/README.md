# Voter Pulse Dashboard

How voters feel and how their lives are going — a single page that lets a
non-technical reader answer "is the country going well?" in 10 seconds.

Listens on `:7062`. Subdomain: `pulse.narve.ai` (registered in
`gateway/config.json`).

## What's on the page

1. **National mood gauge** — composite 0–100 score derived from the three
   sub-indices below, with a verbal label (Bleak / Sour / Strained / Okay /
   Good).
2. **Pocketbook** — Headline CPI, Food CPI, regular-grade gas price, 30-yr
   mortgage rate, Case-Shiller home-price index. Each card shows the latest
   reading, YoY change, and a 60-period sparkline.
3. **Jobs & wages** — Unemployment rate (UNRATE), real median weekly
   earnings (LES1252881600Q), non-farm payrolls (PAYEMS), real disposable
   personal income (DSPIC96).
4. **What people say** — University of Michigan Consumer Sentiment Index
   (UMCSENT) and 1-year inflation expectations (MICH). UMCSENT is rescaled
   to a 0–100 mood contribution using its long-run range.

Each card also surfaces a **4-year delta** badge — the comparison voters
actually make at election time. The hero panel adds an
**inflation-expectations gap** footnote (UMich 1y minus realised CPI YoY)
to capture the "fear gap" that often drives sentiment.
5. **Polymarket — the political mood** — live sentiment-relevant markets
   (right track / wrong track, presidential approval, recession odds,
   inflation/unemployment milestones, election outcomes), bucketed into
   categories and sorted by 24h volume.

The misery index (UNRATE + CPI YoY) is shown as a footnote — the single
backwards-looking number that tracks "how it feels" most reliably.

## Data sources

| What | Source | Cadence |
|---|---|---|
| Headline CPI (CPIAUCSL) | FRED `fredgraph.csv` | monthly |
| Food CPI (CPIUFDSL) | FRED | monthly |
| Gas price (GASREGW) | FRED | weekly |
| 30-yr mortgage (MORTGAGE30US) | FRED | weekly |
| Home price index (CSUSHPISA) | FRED | monthly |
| Unemployment (UNRATE) | FRED | monthly |
| Real median weekly earnings (LES1252881600Q) | FRED | quarterly |
| Non-farm payrolls (PAYEMS) | FRED | monthly |
| Real disposable personal income (DSPIC96) | FRED | monthly |
| Consumer sentiment (UMCSENT) | FRED | monthly |
| Inflation expectations 1y (MICH) | FRED | monthly |
| Sentiment markets | Polymarket Gamma API | live (5 min cache) |

All FRED series are pulled from the public CSV endpoint — no API key
required. Polymarket Gamma is also unkeyed.

## Mood-index methodology

The composite is the equal-weight mean of three sub-scores, each scaled to
0–100 (higher = better):

- **Pocketbook**: linear scoring of CPI YoY, food CPI YoY, gas pump price,
  and the 30-yr mortgage rate against bands chosen to match how voters
  actually react (e.g. CPI YoY 0% → 100, 4% → 50, 8%+ → 0).
- **Jobs**: unemployment rate (3% → 100, 6% → 50, 9%+ → 0) blended with
  real-wage YoY (-2% → 0, 0 → 50, +2%+ → 100).
- **Sentiment**: UMich CSI rescaled against its 1978-present range
  (50 → 0, 110 → 100).

We deliberately avoid overfit weights or time-series anchoring — the goal
is a legible read, not a forecast.

## Run locally

```bash
pip install -r requirements.txt
DEV_MODE=1 python3 server.py
# → http://localhost:7062
```

## Endpoints

- `GET /api/summary` — single page-load payload (mood + life + markets)
- `GET /api/mood` — just the composite score and sub-scores
- `GET /api/life` — every FRED indicator (cached 12h)
- `GET /api/markets` — Polymarket sentiment markets (cached 5 min)
- `GET /healthz` — liveness

Every API endpoint accepts `?force=true` to bypass the cache.

## Container

```bash
docker build -t voter-pulse-dashboard .
docker run --rm -p 7062:7062 -e DEV_MODE=1 voter-pulse-dashboard
```

## Notes

- Auth: same gateway-SSO middleware as `centralbank-dashboard` and
  `world-state-dashboard`. Without `GATEWAY_SSO_SECRET` set, every request
  except `/healthz` returns 503 unless `DEV_MODE=1`.
- The Polymarket category list is rule-based and intentionally narrow.
  Markets that don't match any of `approval / right_track / recession /
  inflation / unemployment / election` are dropped, and a small reject list
  filters out sports/crypto/weather noise that occasionally leaks through
  the politics tags.
- The sub-score band cutoffs are easy targets to tune as we get reader
  feedback. They live in `analysis/mood_index.py`.

# Top Traders Dashboard — Whale Tracking

Tracks the top traders on Polymarket, streams their recent trades, and scans
for suspicious patterns. Uses public, unauthenticated Polymarket APIs.

Port: **8052**. Lives behind the gateway at `traders.narve.ai` in production.

## Run locally

```bash
cd top-traders-dashboard
cp .env.example .env       # optional in DEV_MODE=1
pip install -r requirements.txt
python3 server.py
# http://localhost:8052
```

Or via Docker from the repo root:

```bash
docker compose up --build top-traders
```

## Files in this directory

**Python**
| File | Purpose |
|---|---|
| `server.py` | FastAPI app — leaderboard polling, trade streaming, in-memory 20s cache, gateway SSO middleware. |
| `resolved_markets.py` | Retroactive insider detection. Pulls recently closed markets, finds wallets that bought the winning outcome at long-shot prices, identifies repeat winners. |
| `suspicious_trades.py` | Multi-signal scanner: potential profit, timing-before-close, volume spikes, first-trade wallets, coordinated wallets, statistical outliers, new-account + long-shot patterns. |
| `bayesian_wallets.py` | Beta(α, β) skill estimation per wallet. Posterior mean + high-confidence flag for "this wallet has insider edge". Persists to `bayesian_wallets.db`. |
| `wallet_ml.py` | Two-model wallet anomaly detection: Isolation Forest (unsupervised) + XGBoost ranker (weakly supervised on resolved-market labels). Gracefully degrades if sklearn/xgboost missing. |

**Frontend / data**
| File | Purpose |
|---|---|
| `index.html` | Single-file dashboard UI served by `server.py` at `/`. |
| `bayesian_wallets.db` | Persistent Beta-distribution priors for wallet skill (read/written by `bayesian_wallets.py`). |
| `cache/` | Subdirectory of rolling suspicious-trades JSON snapshots (see `cache/README.md`). |

**Build / config**
| File | Purpose |
|---|---|
| `Dockerfile` | Container build for the `top-traders` service. |
| `.dockerignore` | Excludes `cache/`, `*.db`, logs from the Docker build context. |
| `requirements.txt` | Python deps. |
| `.env.example` | Reference for env vars. Copy to `.env` to use. |
| `README.md` | This file. |

## Data sources

| API | Purpose |
|---|---|
| `https://lb-api.polymarket.com/volume?window=<all\|1d\|7d\|30d>&limit=N` | Leaderboard ranked by volume traded |
| `https://data-api.polymarket.com/trades?user=<wallet>&limit=N` | Recent trades for a given proxy wallet |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_SSO_SECRET` | unset | Required when running behind the gateway. Must match `gateway/.env`. |
| `DEV_MODE` | unset | Set to `1` to bypass gateway auth for local dev. |

## Notes

- Cache TTL is 20s — shorter than the 30s frontend poll to avoid serving stale data.
- When neither `GATEWAY_SSO_SECRET` nor `DEV_MODE=1` is set, the server rejects every request.

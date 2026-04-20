# Deploying the Annoyance Dashboard

The 7th sibling dashboard. Sits on port **8053** (prod slot, dark until
launch day) / **8054** (staging) on the same host as the gateway, behind
Cloudflare Tunnel.

Hostnames:
- **Staging** → `https://annoyance-staging.narve.ai`
- **Prod**    → `https://annoyance.narve.ai` — **DNS entry is NOT added
  during this deploy.** Prod subdomain is a launch-day flip; leave it off
  the tunnel until P8 signs off on the 24h soak.

---

## STRICT ORDER — do not reorder

The dashboard process must be running and observable BEFORE the gateway
learns about it. Shipping gateway/config.json first would route real
user traffic to a nonexistent backend and 502 every `/api/*` call.
Equally, the prod subdomain gets flipped LAST — never before the soak.

```
1. scp annoyance-dashboard/ files to server
2. Launch on :8054 (staging), verify /healthz locally on server
3. DNS: add annoyance-staging.narve.ai → tunnel → :8054
4. 24h soak with EMAIL_NOTIFICATIONS_ENABLED=false
5. ONLY THEN scp updated gateway/config.json
6. fuser -k 7000/tcp; relaunch gateway
7. Commit on server (project memory)
8. DNS: add annoyance.narve.ai (prod) — launch day only, not now
```

**Never ship gateway/config.json before step 2.**
**Do NOT add the prod `annoyance.narve.ai` DNS row in this session.**

Every `scp` here names files explicitly. **Do not rsync with multiple
sources** — prior sessions have clobbered unrelated files this way.

---

## 0. Pre-flight on your laptop

```bash
cd /Users/shocakarel/Habbig/annoyance-dashboard

# 1. Unit tests must be green before you touch the server
python3 -m pytest tests/unit -v

# 2. Syntax check
python3 -m py_compile server.py auth.py observability.py rate_limiter.py

# 3. Confirm HOST is localhost (gateway is the only ingress)
grep "^HOST" config.py
# → HOST = os.environ.get("HOST", "127.0.0.1")
```

If any of those fail, stop. Do not continue.

---

## Step 1 — scp annoyance-dashboard/ files to server

From your laptop, copy each file explicitly. These are the only files
P1 owns — the classifier, sources, and frontend are owned by other tracks
and scp'd separately.

```bash
# Platform/auth layer
scp auth.py          server:~/Habbig/annoyance-dashboard/auth.py
scp observability.py server:~/Habbig/annoyance-dashboard/observability.py
scp rate_limiter.py  server:~/Habbig/annoyance-dashboard/rate_limiter.py

# Server wiring
scp server.py        server:~/Habbig/annoyance-dashboard/server.py

# Scripts + runbook
scp scripts/start.sh server:~/Habbig/annoyance-dashboard/scripts/start.sh
scp scripts/stop.sh  server:~/Habbig/annoyance-dashboard/scripts/stop.sh
scp DEPLOY_ANNOYANCE.md server:~/Habbig/annoyance-dashboard/DEPLOY_ANNOYANCE.md
```

**Do NOT scp gateway/config.json yet.** That's step 5, after the soak.

### Install the staging env file on the server

```bash
ssh server
cat > ~/.annoyance_env_staging <<'EOF'
PRODUCTION=1
HOST=127.0.0.1
PORT=8054
ENVIRONMENT=staging
APP_VERSION=0.1.0
ANTHROPIC_API_KEY=sk-ant-...
GATEWAY_SSO_SECRET=...              # same value as the gateway's env
SENTRY_DSN_ANNOYANCE=https://....ingest.sentry.io/...
LOG_LEVEL=INFO
EMAIL_NOTIFICATIONS_ENABLED=false   # required during soak
EOF
chmod 600 ~/.annoyance_env_staging
```

`GATEWAY_SSO_SECRET` must match the gateway's env var of the same name —
otherwise every `/api/*` request 402s.

---

## Step 2 — Launch on :8054, verify /healthz locally on server

```bash
ssh server
cd ~/Habbig/annoyance-dashboard

# Kill anything on 8054 (safe no-op if empty)
fuser -k 8054/tcp 2>/dev/null || true
sleep 2

# Boot via env file
nohup env $(cat ~/.annoyance_env_staging | xargs) \
    python3 server.py > /tmp/annoyance-staging.log 2>&1 &

sleep 3

# Verify locally on the server — before any DNS exists
curl -sS http://127.0.0.1:8054/healthz
#   → {"status":"ok","db":"...","has_api_key":true}

# /api/* must 402 without SSO headers (prove the paywall is live)
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8054/api/index
#   → 402
```

If /healthz isn't 200, tail `/tmp/annoyance-staging.log` and fix before
touching DNS. Do not proceed to step 3 with a broken process — the
tunnel would ingest 5xx errors into Sentry as real signal.

---

## Step 3 — DNS: add annoyance-staging.narve.ai → tunnel → :8054

Cloudflare → `narve.ai` → DNS → add:

| Type  | Name                | Target (tunnel hostname)         | Proxy |
| ----- | ------------------- | -------------------------------- | ----- |
| CNAME | `annoyance-staging` | `<tunnel-uuid>.cfargotunnel.com` | ✅    |

**Do not add `annoyance` (prod) yet.** That's step 8, launch day only.

Then edit `~/.cloudflared/config.yml` on the server and insert the
staging ingress rule **before** the final `http_status:404` catch-all:

```yaml
ingress:
  # … existing dashboards …

  - hostname: annoyance-staging.narve.ai
    service: http://localhost:8054

  # DO NOT add annoyance.narve.ai here yet. Launch-day only.

  # catch-all MUST stay last
  - service: http_status:404
```

Reload the tunnel:
```bash
ssh server 'sudo systemctl reload cloudflared'
```

Smoke-test end-to-end (laptop, traffic through Cloudflare):

```bash
# 1. Health endpoint is public
curl -sS https://annoyance-staging.narve.ai/healthz
#   → 200 OK, JSON body

# 2. /api/* without SSO headers must 402
curl -sS -o /dev/null -w '%{http_code}\n' \
    https://annoyance-staging.narve.ai/api/index
#   → 402

# 3. /api/* with fake gateway secret must still 402
curl -sS -o /dev/null -w '%{http_code}\n' \
    -H 'X-Gateway-Secret: wrong' \
    https://annoyance-staging.narve.ai/api/index
#   → 402

# 4. Dashboard HTML renders
curl -sS https://annoyance-staging.narve.ai/ | head -20
#   → starts with <!DOCTYPE html>
```

---

## Step 4 — 24h soak with EMAIL_NOTIFICATIONS_ENABLED=false

Keep the staging process running for a full 24-hour window. Do NOT flip
email notifications on during soak — we don't want a misconfigured
spike detector spraying Pro subscribers before a human has reviewed
what it's producing.

During soak, watch:
- **Sentry** — baseline error volume should be near zero. Anything new
  is blocking.
- **`/healthz`** — external monitor (curl in cron) must return 200
  every minute for the whole window.
- **Cost ceiling** — `curl http://127.0.0.1:8054/admin/cost-summary` on
  the server should show `today_cents` well below
  `DAILY_COST_CEILING_CENTS` (default 1000 = $10/day).
- **Spike distribution** — per DECISIONS.md #9, target is 5–10/day.
  Inspect `spikes` table to confirm we're in that range, not 0 (quiet
  bug) and not 100 (runaway detector).
- **Logs** — tail `logs/annoyance.log`. Look for `classifier_loop:
  cost ceiling hit` warnings — that's the only condition that should
  EVER downgrade the loop.

If any of those drift, **abort**. Debug on staging before moving on.
Re-start the 24h soak clock from zero.

---

## Step 5 — ONLY THEN scp updated gateway/config.json

```bash
# From the laptop, only after step 4 has cleanly passed:
scp ../gateway/config.json server:~/Habbig/gateway/config.json
```

The addition is a single new object under `"dashboards"` keyed
`"annoyance"` with `"target": 8053`. Existing 6 dashboards are
untouched. Verify on the server:

```bash
ssh server
cd ~/Habbig/gateway
python3 -c "import json; print(list(json.load(open('config.json'))['dashboards'].keys()))"
# → ['sports','weather','world','crypto','midterm','top_traders','annoyance']
```

**If the key order or any other dashboard entry was changed**, abort and
investigate — something else clobbered the file.

---

## Step 6 — fuser -k 7000/tcp; relaunch gateway

The gateway process loads config.json at boot and caches the routes.
It has to restart before the new "annoyance" entry is live.

```bash
ssh server
cd ~/Habbig/gateway

fuser -k 7000/tcp 2>/dev/null || true
sleep 2

# Match the gateway's existing env-file-based launch.
nohup env $(cat ~/.gateway_env | xargs) \
    python3 server.py > /tmp/gateway.log 2>&1 &

sleep 3
curl -sS http://127.0.0.1:7000/healthz
```

The gateway is now aware of "annoyance" on port 8053. Prod subdomain
still doesn't exist in DNS — staging keeps serving on 8054 unchanged.

---

## Step 7 — Commit on server (project memory)

Per project memory: every deploy commits on the server immediately so
the next deploy starts from a clean state.

```bash
ssh server
cd ~/Habbig/annoyance-dashboard
git add -A
git status
git commit -m "deploy: annoyance P1 staging (SSO + paywall + rate limit + observability)"

cd ~/Habbig/gateway
git add config.json
git commit -m "config: register annoyance dashboard on 8053"
```

Do **not** skip this. A previous session deployed crypto-dashboard
changes without committing and the next deploy clobbered them.

---

## Step 8 — DNS: add annoyance.narve.ai (prod) — launch-day only

**This step is NOT part of the current deploy session. Skip it now.**

When launch day arrives (after P8 sign-off on the 24h+ staging soak):

1. Free port 8053 on the server: `fuser -k 8053/tcp && sleep 2`
2. Boot prod env: `nohup env $(cat ~/.annoyance_env | xargs) python3 server.py > /tmp/annoyance.log 2>&1 &`
3. Smoke-test locally: `curl -sS http://127.0.0.1:8053/healthz`
4. Add Cloudflare CNAME `annoyance` → `<tunnel-uuid>.cfargotunnel.com`,
   proxy ON.
5. Add ingress rule to `~/.cloudflared/config.yml`:
   ```yaml
     - hostname: annoyance.narve.ai
       service: http://localhost:8053
   ```
   (Still before the final `http_status:404` catch-all.)
6. `sudo systemctl reload cloudflared`
7. Re-run the smoke tests from step 3 against `annoyance.narve.ai`
   (no `-staging`).

---

## Rollback

If anything misbehaves during or after step 5:

```bash
ssh server

# 1. Revert the gateway config change immediately — this is the
#    high-blast-radius step, any other dashboard breaking routes here.
cd ~/Habbig/gateway
git log --oneline -n 5     # find the commit before "register annoyance"
git checkout <SHA> -- config.json
fuser -k 7000/tcp && sleep 2
nohup env $(cat ~/.gateway_env | xargs) \
    python3 server.py > /tmp/gateway.log 2>&1 &

# 2. Kill the annoyance process on 8054 — staging stops getting
#    traffic. DNS stays, the tunnel just returns 502 for the
#    annoyance-staging subdomain, which is fine.
fuser -k 8054/tcp
```

Cloudflare Tunnel ingress does not need to change on rollback — as long
as the subdomain points at a dead port, the tunnel returns 502 and the
error isolates to the staging subdomain alone.

If the tunnel itself needs to disconnect (catastrophic failure),
`ssh server 'sudo systemctl stop cloudflared'` — all dashboards go down
so only do this if the whole box is on fire.

---

## Post-deploy checklist (end of step 7)

- [ ] `/healthz` returns 200 through the staging tunnel
- [ ] `/api/index` returns 402 without SSO headers
- [ ] `/api/index` returns 200 with a real pro-tier session via the gateway
- [ ] Sentry project has the release tagged `0.1.0`
- [ ] BetterStack (if configured) is receiving JSON logs
- [ ] `/admin/cost-summary` shows today's spend
- [ ] Both git repos (`gateway` + `annoyance-dashboard`) committed on server
- [ ] `EMAIL_NOTIFICATIONS_ENABLED=false` in the staging env file
- [ ] Prod `annoyance.narve.ai` DNS is still UNSET (step 8 is launch day)

---

## Appendix A — Env var reference

| Var                             | Required | Notes                                            |
| ------------------------------- | :------: | ------------------------------------------------ |
| `HOST`                          |    ✓     | Must be `127.0.0.1`. Startup asserts on this.    |
| `PORT`                          |    ✓     | 8054 staging / 8053 prod (launch day only)       |
| `GATEWAY_SSO_SECRET`            |    ✓     | Shared with gateway. Without it all `/api/*` 402s. |
| `ANTHROPIC_API_KEY`             |          | Classifier no-ops without it. Dashboard still boots. |
| `SENTRY_DSN_ANNOYANCE`          |          | Dashboard-specific DSN. Falls back to `SENTRY_DSN`. |
| `ENVIRONMENT`                   |          | `production` / `staging`. Tagged on Sentry events. |
| `APP_VERSION`                   |          | Release tag for Sentry.                          |
| `LOG_LEVEL`                     |          | `INFO` default. `DEBUG` for incident triage.     |
| `RATE_LIMIT_ENABLED`            |          | `false` disables rate limiting (tests only).     |
| `PAYWALL_UPGRADE_URL`           |          | Override for the 402 payload (defaults to narve.ai/billing). |
| `EMAIL_NOTIFICATIONS_ENABLED`   |          | **Must be `false` during soak.** P8-owned flag.  |

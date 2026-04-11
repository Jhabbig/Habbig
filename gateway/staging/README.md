# Staging environment

`staging.narve.ai` — a full mirror of production, running on the same server
with an isolated SQLite database and separate secrets.

## Architecture

Production and staging share a host. They're separated by:

| Layer | Production | Staging |
|---|---|---|
| Hostname | `narve.ai` | `staging.narve.ai` |
| Uvicorn port | 7000 | 7001 |
| SQLite file | `auth.db` | `auth-staging.db` |
| Env file | `~/.gateway_env` | `~/.gateway_env_staging` |
| Systemd unit | (none — nohup) | (none — nohup) |
| Cloudflare Tunnel ingress | `http://localhost:7000` | `http://localhost:7001` |

A single `cloudflared` process routes both hostnames based on `config.yml`.

## Why same-host staging?

The real deployment is a single-VM nohup uvicorn setup, not Docker. Spinning
up a second VM just for staging would triple infrastructure cost for a
project this size. Running staging as a second uvicorn on the same host
gives us the guarantees we actually need:

- **Isolated data** — staging can never corrupt production data because it
  reads/writes a different file.
- **Isolated secrets** — different `SITE_ACCESS_TOKEN`, different
  `CREDENTIALS_ENCRYPTION_KEY`, Stripe test keys only.
- **Free rollback test surface** — bad code never reaches prod until it's
  verified working on staging.

What we explicitly don't get from same-host staging:
- **Infrastructure drift catching** — an OS upgrade on prod would break
  staging too. Mitigate by bringing staging down during OS work.
- **Hardware failure isolation** — if the host dies, both environments die.
  That's acceptable until the site has real traffic.

## Setup (first time on the server)

```bash
# 1. Create the staging env file on the server
ssh julianhabbig@100.69.44.108
cp ~/.gateway_env ~/.gateway_env_staging

# 2. Edit the staging env — fill in a fresh SITE_ACCESS_TOKEN and
#    CREDENTIALS_ENCRYPTION_KEY (see staging/.env.staging for the full list)
nano ~/.gateway_env_staging

# 3. Add the staging ingress rule to Cloudflare Tunnel (once, manual edit)
sudo nano /etc/cloudflared/config.yml
# Add this block BEFORE the `*.narve.ai` wildcard entry:
#   - hostname: staging.narve.ai
#     service: http://localhost:7001
sudo systemctl restart cloudflared

# 4. Deploy the staging gateway for the first time
#    (from your laptop — runs the scp + nohup boot sequence)
bash scripts/deploy-staging.sh
```

See `CLOUDFLARE_CHANGES.md` for the full DNS / ingress / WAF checklist.

## Daily usage

```bash
# Deploy latest code to staging
bash scripts/deploy-staging.sh

# Verify staging is healthy
curl -s https://staging.narve.ai/health | python3 -m json.tool

# Tail staging logs
ssh julianhabbig@100.69.44.108 "tail -f /tmp/gateway_staging.log"

# Stop staging only (leaves production alone)
ssh julianhabbig@100.69.44.108 "fuser -k 7001/tcp"
```

## Never on staging

- **Live Stripe keys** — use `sk_test_` only.
- **Real user emails** — `EMAIL_DRY_RUN=true` is hard-wired into the env.
- **Production database path** — staging must never open `auth.db`. The
  `GATEWAY_DB_PATH=auth-staging.db` var enforces this.
- **Same `SITE_ACCESS_TOKEN` as prod** — sharing the staging URL with a QA
  tester must not grant them prod access.

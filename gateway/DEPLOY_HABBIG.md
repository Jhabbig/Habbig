# Deploying to habbig.com — Step-by-Step Checklist

Everything you need to put this gateway online at `habbig.com` with HTTPS,
no public IP required, no port forwarding, and all 7 dashboards accessible
at their own subdomain. Uses Cloudflare Tunnel, which is free and matches
how you're already sharing dashboards with your father.

Estimated time: **~45 minutes**, most of that is waiting on DNS.

---

## Host variable

The runbook commands below reference `$NARVE_HOST` instead of hardcoding the
Tailscale IP. Export it once at the top of your shell session so copy-paste
works verbatim. Find the value with `tailscale ip -4` on the host:

```bash
export NARVE_HOST="<your-tailscale-ipv4>"   # e.g. 100.x.y.z
# or a Tailscale MagicDNS name
export NARVE_HOST="narve.your-tailnet.ts.net"
```

Treat the Tailscale IP as sensitive-ish — do not commit it to public docs
or tickets; it reveals your tailnet topology.

---

## 0. What you'll need before starting

- [ ] The `habbig.com` domain (purchased, see step 1)
- [ ] A free [Cloudflare](https://dash.cloudflare.com/sign-up) account
- [ ] `cloudflared` installed on the machine that will host the dashboards
  - macOS: `brew install cloudflared`
  - Ubuntu: `curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared`
- [ ] The dashboards booting cleanly via `./start_dashboards.sh` on that machine

---

## 1. Buy habbig.com

Fastest path: **Cloudflare Registrar**.

1. Go to https://dash.cloudflare.com → **Domain Registration → Register Domains**
2. Search `habbig.com`, add to cart, checkout (Cloudflare charges at-cost, usually $8–10/yr for `.com`)
3. Done — DNS is auto-wired to Cloudflare, no nameserver changes needed

**If you buy it elsewhere** (Namecheap, Porkbun, GoDaddy, etc.):

1. Purchase `habbig.com` on the registrar of your choice
2. In Cloudflare, click **Add a Site → habbig.com** (Free plan)
3. Cloudflare gives you two nameservers like `alice.ns.cloudflare.com` / `bob.ns.cloudflare.com`
4. Go back to your registrar, replace the existing nameservers with those two
5. Wait 5 min – 24 hr for Cloudflare to show "Active"

---

## 2. Authenticate cloudflared

On the machine that will run the dashboards:

```bash
cloudflared tunnel login
```

This opens a browser, log in to Cloudflare, pick `habbig.com`, click **Authorize**.
A certificate lands in `~/.cloudflared/cert.pem`.

---

## 3. Create the tunnel

```bash
cloudflared tunnel create habbig-gateway
```

Output includes a tunnel UUID, e.g.:

```
Tunnel credentials written to /Users/julianhabbig/.cloudflared/3f2a8e1c-....json
Created tunnel habbig-gateway with id 3f2a8e1c-4b1d-4a3f-9e8a-abcdef012345
```

**Save the tunnel ID** — you'll paste it into the next two steps.

### Lock down the tunnel credential file

The JSON credential file written under `~/.cloudflared/<uuid>.json` is a
long-lived tunnel secret — anyone who reads it can impersonate your tunnel.
Move it to a system-owned path and tighten perms:

```bash
sudo mkdir -p /etc/cloudflared
sudo mv ~/.cloudflared/*.json /etc/cloudflared/
sudo chown root:root /etc/cloudflared/*.json
sudo chmod 600 /etc/cloudflared/*.json
```

Update `credentials-file:` in the next step to point at the new path.

---

## 4. Write the ingress config

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: 3f2a8e1c-4b1d-4a3f-9e8a-abcdef012345   # <-- paste your tunnel ID
credentials-file: /Users/julianhabbig/.cloudflared/3f2a8e1c-4b1d-4a3f-9e8a-abcdef012345.json

ingress:
  # Wildcard — every subdomain goes to the gateway, which then routes by Host header
  - hostname: "*.habbig.com"
    service: http://localhost:7000
    originRequest:
      noTLSVerify: true
      connectTimeout: 10s

  # Apex — the landing page / login / billing
  - hostname: "habbig.com"
    service: http://localhost:7000
    originRequest:
      noTLSVerify: true
      connectTimeout: 10s

  # Fallback
  - service: http_status:404
```

Replace the UUID with yours on **both** `tunnel:` and `credentials-file:` lines.

---

## 5. Register DNS routes for every subdomain

Cloudflare needs a CNAME record pointing each subdomain at the tunnel. Instead
of clicking through the dashboard 8 times, use the helper script:

```bash
./gateway/setup_cloudflare.sh 3f2a8e1c-4b1d-4a3f-9e8a-abcdef012345
```

That runs `cloudflared tunnel route dns` for `habbig.com` + all 7 subdomains
defined in `gateway/config.json`. If a route already exists, it'll say so and
move on — safe to re-run.

---

## 6. Flip to production mode

On the host machine:

```bash
# Generate a strong cookie secret (save this somewhere safe)
export GATEWAY_COOKIE_SECRET="$(openssl rand -hex 32)"
export PRODUCTION=1

# Or add to ~/.zshrc / ~/.bashrc so it persists across reboots
echo 'export PRODUCTION=1' >> ~/.zshrc
echo "export GATEWAY_COOKIE_SECRET='$GATEWAY_COOKIE_SECRET'" >> ~/.zshrc
```

`PRODUCTION=1` does two things:
1. **Disables the localhost dev bypass** — no more auto-login, real signup is required
2. **Flips session cookies to `secure=True`** — requires HTTPS (Cloudflare provides it)

---

## 7. Start everything

Two processes need to run: your dashboards (+ gateway) and `cloudflared`.

**Terminal 1 — dashboards + gateway:**

```bash
cd "/Users/julianhabbig/Claude Vibecoding /Polymarket"
./start_dashboards.sh restart
```

Check: `curl http://localhost:7000/login` should return HTML.

**Terminal 2 — cloudflared:**

```bash
cloudflared tunnel run habbig-gateway
```

You should see log lines like `Registered tunnel connection` × 4 (Cloudflare
opens 4 connections for redundancy).

---

## 8. Verify

From **any other device** (not the host machine):

1. Visit `https://habbig.com` — should show the login page with a valid
   HTTPS padlock (Cloudflare's certificate)
2. Click **Sign up**, create an account with a real email + 8+ char password
3. Go to **Billing**, click **Monthly $9.99** under Crypto Edge
4. Go to **My Dashboards**, click **Open →** on the Crypto Edge card
5. You should land at `https://crypto.habbig.com` showing the crypto dashboard
6. Open the other subdomains to verify each one proxies correctly

---

## 9. Run cloudflared as a service (optional but recommended)

Otherwise you lose the tunnel when you close the terminal or reboot.

**macOS (launchd):**

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

**Ubuntu (systemd):**

```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
sudo systemctl status cloudflared
```

Do the same for `start_dashboards.sh` if you want auto-start; on Ubuntu the
cleanest path is a systemd unit that runs the script at `multi-user.target`.

---

## 10. Backups

The SQLite DB at `gateway/auth.db` holds users, subscriptions, API keys,
encrypted exchange credentials, and push tokens. Losing it is a full data
loss event. A naive `cp` while the server is writing is unsafe — use
`sqlite3 .backup` which takes a consistent snapshot, then encrypt and
ship off-site.

Install a daily cron (`crontab -e` as the gateway user):

```bash
# /etc/cron.d/narve-backup — runs 03:17 UTC daily
17 3 * * * narve /usr/local/bin/narve-backup.sh >> /var/log/narve-backup.log 2>&1
```

`/usr/local/bin/narve-backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
DEST=/var/backups/narve
mkdir -p "$DEST"
TODAY=$(date +%F)
# 1. Consistent snapshot via the online-backup API (no lock contention).
sqlite3 /home/julianhabbig/Habbig/gateway/auth.db \
    ".backup $DEST/auth-$TODAY.db"
# 2. Encrypt with GPG (passphrase in /etc/narve/backup.passphrase, 0600 root:root).
gpg --batch --yes --symmetric --cipher-algo AES256 \
    --passphrase-file /etc/narve/backup.passphrase \
    "$DEST/auth-$TODAY.db"
rm "$DEST/auth-$TODAY.db"
# 3. Ship off-site (rclone or aws s3 — pick one).
rclone copy "$DEST/auth-$TODAY.db.gpg" "r2:narve-backups/"
# or: aws s3 cp "$DEST/auth-$TODAY.db.gpg" "s3://narve-backups/"
# 4. Local retention: keep 14 days.
find "$DEST" -name "auth-*.db.gpg" -mtime +14 -delete
```

Make it executable and root-only: `chmod 700 /usr/local/bin/narve-backup.sh`.

Test your restore path at least once per quarter — an untested backup is not
a backup.

---

## 11. Aftercare checklist

- [ ] **Backups configured** (see section 10).
- [ ] **Rotate `GATEWAY_COOKIE_SECRET`** if you ever suspect it's leaked. (Note: this will log everyone out.)
- [ ] **Monitor `/tmp/dashboard_*.log`** — the start script writes each service's stdout/stderr there.
- [ ] **Set up `cloudflared metrics`** on `localhost:2000` if you want Prometheus-style health data.
- [ ] **Stripe** — when you're ready to charge money, the DB schema and hooks are already in place; see `gateway/README.md` → "Wiring real Stripe payments later".

---

## Troubleshooting

**`Error 1033` or `Host Error` in browser:**
Cloudflare can't reach your tunnel. Check `cloudflared tunnel run habbig-gateway`
is still running and shows connected status.

**`502 Bad Gateway`:**
Tunnel is up but the gateway on port 7000 isn't. Run
`./start_dashboards.sh status` — the gateway line should say RUNNING.

**Redirects loop between `/` and `/login`:**
`PRODUCTION=1` is set but `secure=True` cookies can't be set over HTTP. Make
sure you're visiting `https://` (not `http://`), and that Cloudflare's SSL mode
is **Full** (Overview → SSL/TLS → set to Full, not Flexible).

**Signup works but cookie doesn't persist:**
The session cookie has `Domain=.habbig.com` — check that your browser is
actually hitting `habbig.com` (not an IP). Also clear any stale `pm_gateway_session`
cookie from previous localhost tests.

**A subdomain shows login instead of the dashboard:**
That account doesn't have a subscription for that dashboard. Go to
`https://habbig.com/billing` and subscribe.

**Dashboard loads but its internal links are broken:**
The dashboard has absolute paths like `/static/foo.css` that aren't namespaced
to its subdomain. Since the gateway proxies each subdomain to its own backend
without a path prefix, this should Just Work — but if a specific dashboard does
something funky like hard-coding `http://localhost:8000`, that would break.
Fix by editing the dashboard's HTML to use relative paths.

**`cloudflared tunnel route dns` says "An A, AAAA, or CNAME record with that host already exists":**
There's a leftover DNS record from a previous tunnel or manual entry. Go to
Cloudflare → DNS → find the offending record → delete it → re-run the setup
script.

---

## What a successful deployment looks like

```
$ ./start_dashboards.sh status

Dashboard Status:
  Port 7000 (Gateway):  RUNNING
  Port 8000 (Crypto):   RUNNING
  Port 8050 (Stock):    RUNNING
  Port 8051 (Midterm):  RUNNING
  Port 8052 (Traders):  RUNNING
  Port 5050 (Weather):  RUNNING
  Port 8888 (Sports):   RUNNING
  Port 7050 (World):    RUNNING

$ curl -s https://habbig.com/login | grep -o '<title>.*</title>'
<title>Sign in — Polymarket Dashboards</title>

$ cloudflared tunnel info habbig-gateway
NAME:     habbig-gateway
ID:       3f2a8e1c-...
CREATED:  2026-04-05 ...
CONNECTIONS:
  ID   CREATED  EDGE        PROTO  CONNECTOR ID
  0    ...      LAX (US)    quic   ...
  1    ...      LAX (US)    quic   ...
  2    ...      SJC (US)    quic   ...
  3    ...      SJC (US)    quic   ...
```

You're live.

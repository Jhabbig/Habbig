# Narve.ai Scraper Service

Standalone data scraper that collects posts from X (Twitter) and TruthSocial
using browser automation — no paid API access required.

Runs alongside the main gateway application on the same server. Communicates
via HTTP on localhost. Fully manageable from the admin panel.

## Legal Note

This scraper is intended for **personal/research use only**. Users should
ensure compliance with each platform's Terms of Service. Rate limits are
applied aggressively to minimise server load and avoid detection.

## Prerequisites

- Python 3.12+
- Playwright (`playwright install chromium`)
- Display server for session setup (X11, VNC, or local desktop)

## First-Time Setup

### 1. Install dependencies

```bash
cd /path/to/gateway
pip install -r scraper/requirements.txt
playwright install chromium
```

### 2. Generate API key

```bash
openssl rand -hex 24
```

Add the key to **both** `.env` files:

**scraper/.env:**
```
SCRAPER_API_KEY=<your-key-here>
MAIN_SERVER_URL=http://localhost:7000
```

**gateway/.env:**
```
SCRAPER_URL=http://127.0.0.1:8001
SCRAPER_API_KEY=<same-key-here>
```

### 3. Set up Twitter session

```bash
python scraper/setup_twitter_session.py
```

This opens a headed browser. Log in to X manually (including 2FA), then
press Enter in the terminal. Session cookies are saved to
`scraper/stealth/profiles/twitter/`.

On a headless server, use X11 forwarding:
```bash
ssh -X user@server python scraper/setup_twitter_session.py
```

### 4. Set up TruthSocial session (optional)

```bash
python scraper/setup_truthsocial_session.py
```

TruthSocial prominent accounts (Trump, etc.) can be scraped without a
session. The session is only needed for keyword search.

### 5. Start the scraper

```bash
bash scraper/start.sh
```

Or manually:
```bash
cd /path/to/gateway
uvicorn scraper.main:app --host 127.0.0.1 --port 8001
```

## Running

```bash
bash scraper/start.sh    # Start
bash scraper/stop.sh     # Stop
```

The scraper runs on `127.0.0.1:8001` (localhost only, not exposed to internet).

## Managing via Admin Panel

The admin panel at `/admin` has a **Scraper** tab with:

- **Status cards** — session validity, last run time, posts collected today
- **Scheduler controls** — pause, resume, trigger jobs, edit intervals
- **Keyword management** — add/remove keywords per platform
- **Run history** — see recent scrape results
- **Session management** — reset sessions (requires re-login on server)

## Session Expiry

Sessions typically last **30 days**. When a session expires:

1. The scraper health status will show the session as invalid
2. The admin panel will reflect this
3. SSH into the server and re-run the setup script:
   ```bash
   python scraper/setup_twitter_session.py
   ```

## Adding Keywords

Via admin panel (recommended) or API:

```bash
curl -X POST http://localhost:8001/keywords \
  -H "Authorization: Bearer $SCRAPER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"platform": "twitter", "keyword": "new keyword"}'
```

## Rate Limits

Aggressive rate limiting is applied to avoid detection:

| Platform    | Between keywords | Max posts/keyword | Schedule     |
|-------------|-----------------|-------------------|--------------|
| Twitter     | 45-55 seconds   | 100               | Every 20 min |
| TruthSocial | 30-40 seconds   | 40                | Every 15 min |

These are configurable via `.env` or the admin panel.

## Architecture

```
scraper/
├── main.py              # FastAPI app (port 8001)
├── scheduler.py         # APScheduler (runs scrape jobs)
├── config.py            # .env loader
├── scrapers/
│   ├── base.py          # Abstract base
│   ├── twitter.py       # X/Twitter (Playwright + XHR interception)
│   └── truthsocial.py   # TruthSocial (HTTP + Playwright fallback)
├── transmission/
│   ├── pusher.py        # Push posts to main server
│   └── receiver.py      # On-demand pull job manager
├── storage/
│   ├── db.py            # Local SQLite
│   └── models.py        # Data models
├── stealth/profiles/    # Saved browser sessions
├── tests/               # Test suite
└── logs/                # Log files
```

## Troubleshooting

### Session expired
Run the setup script again on the server.

### Detection / rate limiting
Increase delays in `.env`. The scraper uses playwright-stealth, random
viewports, random user agents, and realistic scroll behaviour.

### Transmission failures
Check that `SCRAPER_API_KEY` matches in both `.env` files. The admin panel
shows untransmitted post count and retry status.

### Scraper unreachable from admin panel
Ensure the scraper is running (`bash scraper/start.sh`) and that
`SCRAPER_URL` in the gateway `.env` is correct.

## Running Tests

```bash
cd /path/to/gateway
python -m pytest scraper/tests/ -v
```

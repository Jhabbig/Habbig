# Culture Dashboard — State of Culture

Aggregates pop culture, internet culture, and zeitgeist signals across
~16 sources into one composite index.

Port: **7070**. Lives behind the gateway at `culture.narve.ai` in production.

## Run locally

```bash
cd culture-dashboard
cp .env.example .env       # all keys optional — missing keys disable that source
pip install -r requirements.txt
python3 server.py
# http://localhost:7070
```

Or via Docker from the repo root:

```bash
docker compose up --build culture
```

The first refresh kicks on startup in the background — open the page, it'll
populate as scrapers finish (15-60s typical).

## Sections

| Section | What it captures | Sources |
|---|---|---|
| `memes` | Viral content + meme entries | TikTok, Instagram, Reddit (r/memes, r/dankmemes, r/MemeEconomy, …), Know Your Meme |
| `attention` | What people are searching / watching | Google Trends, Wikipedia top pageviews, YouTube #trending, X trending |
| `entertainment` | Charts | Box office (The Numbers), Apple Music top 50, Spotify Top 50 Global, Steam most played |
| `markets` | Culture-bucket prediction markets | Polymarket (pop culture / entertainment / awards / …) |
| `news` | Top headlines + sentiment | BBC, NYT, Guardian, AP, Reuters RSS |
| `language` | Slang + emerging ideas | Urban Dictionary WotD, Substack leaderboards |

The composite **culture index** is a log-scaled, weight-blended 0-100 score
across sections. Tunable in `index_calc.py:CALIBRATION`.

## TikTok / Instagram backends

Both have a pluggable backend, picked at call time:

1. **Apify** (`APIFY_TOKEN`) — recommended. Robust, paid, supports
   `clockworks/tiktok-scraper` and `apify/instagram-scraper` actors.
2. **RapidAPI** (`RAPIDAPI_KEY`) — also paid. Default hosts:
   `scraptik.p.rapidapi.com` and `instagram-scraper-api2.p.rapidapi.com`.
3. **Unofficial libs** — free but fragile. TikTok needs `TIKTOK_MS_TOKEN`
   and the optional `TikTokApi` Python package; Instagram needs login creds
   and the optional `instaloader` package. Accounts may get banned. Don't
   use real accounts you care about.

If none of the above are configured the scraper returns `[]` cleanly — the
dashboard still renders, that section just shows "No data".

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI app + SSO middleware + REST endpoints. |
| `models.py` | `Item` dataclass — uniform shape every scraper returns. |
| `cache.py` | SQLite cache. Atomic per-source replace, freshness tracking. |
| `scheduler.py` | Background refresh loop. Per-source cadence; failures isolated. |
| `index_calc.py` | Composite culture index — log-scaled, weighted. |
| `scrapers/__init__.py` | Registry. Add a new module here to add a source. |
| `scrapers/_http.py` | Shared httpx client + UA. |
| `scrapers/tiktok.py` | TikTok trending — Apify / RapidAPI / unofficial. |
| `scrapers/instagram.py` | Instagram hashtag posts — same backends. |
| `scrapers/reddit_memes.py` | Reddit meme/culture subs (top of day). |
| `scrapers/kym.py` | Know Your Meme RSS. |
| `scrapers/google_trends.py` | Google Trends daily RSS. |
| `scrapers/wikipedia.py` | Wikipedia top pageviews (yesterday). |
| `scrapers/youtube_trending.py` | YouTube Data API #mostPopular. |
| `scrapers/x_trending.py` | X trending (paid backends only). |
| `scrapers/box_office.py` | Weekend box office (The Numbers). |
| `scrapers/music_charts.py` | Apple Music top 50. |
| `scrapers/spotify_charts.py` | Spotify Top 50 Global. |
| `scrapers/steam_top.py` | Steam most-played. |
| `scrapers/markets.py` | Polymarket culture-bucket events. |
| `scrapers/news.py` | News RSS + dictionary-sentiment. |
| `scrapers/urban_dictionary.py` | Urban Dictionary WotD. |
| `scrapers/substack.py` | Substack section leaderboards. |
| `index.html` | Single-page UI, vanilla JS, no build step. |
| `Dockerfile` | Container build for the `culture` service. |
| `requirements.txt` | Python deps. |
| `.env.example` | Reference for env vars. |

## API

| Endpoint | Returns |
|---|---|
| `GET /api/index` | Composite + per-section scores, calibration. |
| `GET /api/section/{section}` | Top items in a section. |
| `GET /api/source/{source}` | Top items from one source (debug). |
| `POST /api/refresh?source=…` | Kick all scrapers (or one). |
| `GET /api/health` | Per-source freshness + last error. |
| `GET /healthz` | Liveness (unauthenticated). |

## Adding a source

1. Drop a module in `scrapers/` exposing `NAME`, `SECTION`,
   `REFRESH_SECONDS`, and `async def fetch() -> list[Item]`.
2. Import + add it to the list in `scrapers/__init__.py:registry()`.
3. (Optional) Tune `index_calc.py:CALIBRATION` if the new source changes
   typical signal volume for its section.

## ToS / legal

TikTok and Instagram both prohibit scraping in their ToS. The unofficial
backends are best-effort dev paths; for production use a paid backend or
take the data from a licensed provider.

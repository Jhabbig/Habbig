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
| `lifestyle` | Books / fashion / search trends | NYT bestsellers, Lyst Index (quarterly), Pinterest trends |

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
| `cache.py` | SQLite cache. Atomic per-source replace, freshness tracking, phash, index history, item history, surge alerts. |
| `scheduler.py` | Background refresh loop + phash worker + index-history snapshotter + surge/alert worker. |
| `dedup.py` | Perceptual-hash worker + greedy clustering. |
| `index_calc.py` | Composite culture index — log-scaled, weighted. |
| `surge_calc.py` | Per-item z-score against trailing window — picks the rising items. |
| `topics.py` | Cross-source topic clustering — Jaccard on title tokens + hashtags. |
| `edge.py` | Topic ↔ Polymarket-market matcher; computes mispricing score from surge vs. price velocity. |
| `price_velocity.py` | Trailing 24h price velocity (mid-price) + downsampled trajectory per event. |
| `backtest.py` | Predictive validation: hit-rate of historical market-source surges vs realised 24h price moves. |
| `digest.py` | Calls Claude (anthropic SDK) with a packed snapshot to produce the daily culture brief. |
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
| `scrapers/nyt_bestsellers.py` | NYT bestsellers (API or HTML). |
| `scrapers/lyst_index.py` | Lyst Index quarterly fashion ranking. |
| `scrapers/pinterest_trends.py` | Pinterest trends (extracted from page JSON). |
| `index.html` | Single-page UI + sparkline + dupes badges, vanilla JS, no build step. |
| `Dockerfile` | Container build for the `culture` service. |
| `requirements.txt` | Python deps. |
| `.env.example` | Reference for env vars. |

## Cross-platform meme dedup

Items with images get a 64-bit perceptual hash (`imagehash.dhash`) computed
in the background after each scraper sweep. At read time `/api/section/{s}`
groups items whose hashes are within Hamming distance 8 (tunable via
`CULTURE_PHASH_MAX_DISTANCE`) — the highest-scoring item becomes the
representative, the rest land in `extra.dupes` as `[{source, url, title}, …]`
so the UI can render "also on tiktok, reddit_memes …".

`Pillow` and `imagehash` are required deps; if either is missing the dedup
logic becomes a no-op and items pass through ungrouped.

## Culture index history

The composite index is snapshotted every 10 minutes into the
`index_history` table. `GET /api/index/history?hours=72` returns the last
N hours of points. The dashboard renders a 60×200 SVG sparkline next to the
current score.

## Surge alerts

After each scraper sweep, every item's score is appended to `item_history`.
Once an item has ≥4 points in the last 7 days, the surge worker computes a
z-score against its trailing window. Items with positive z are returned by
`GET /api/surges`, ranked highest first; the dashboard renders the top 8 in
a "Rising now" panel with per-item trajectory sparklines.

Only useful for **recurring** items — Spotify chart positions, Wikipedia
pageviews, NYT bestsellers, Lyst ranks, Pinterest trends, Polymarket
markets. One-shot items (Reddit posts, news headlines, KYM new entries)
won't generate surges since there's no baseline.

If `SURGE_WEBHOOK_URL` is set, items crossing `SURGE_Z_THRESHOLD`
(default 2.5) trigger a webhook POST with a per-item cooldown
(`SURGE_ALERT_COOLDOWN_HOURS`, default 6) so one sustained surge
doesn't spam.

`item_history` is auto-pruned to the last 7 days every cycle.

## Today-in-culture digest (LLM)

If `ANTHROPIC_API_KEY` is set, a background worker calls Claude every hour
(`CULTURE_DIGEST_INTERVAL` seconds) and packs the current dashboard state
(composite index, surges, topics, edges, top news/memes) into a single
JSON snapshot. Claude returns a 150-200 word markdown digest rendered in
the panel at the top of the page.

Default model is `claude-haiku-4-5` (cheapest). Flip to `claude-opus-4-7`
via `CULTURE_DIGEST_MODEL` for richer prose. The system prompt carries a
1-hour `cache_control` marker for forward compatibility — it doesn't
engage at the current prompt size, but will if the prompt grows past the
model's cache-min threshold (≥4096 tokens on Haiku 4.5 / Opus 4.7).

`POST /api/digest/refresh` regenerates on demand; the dashboard's
"Regenerate" button fires this. Per-digest token counts (input, output,
cache read, cache create) are persisted in the `digests` table for cost
auditing.

## Cross-platform topic clustering

`topics.py` runs greedy centroid-based Jaccard clustering on item titles
(plus hashtags + extra strings) across every section. A cluster is kept
only if it spans ≥2 distinct sources — single-source clusters aren't
really cross-platform topics.

Threshold tunable via `CULTURE_TOPIC_MIN_OVERLAP` (default 0.30).

`GET /api/topics` returns clusters ranked by spread, then total score,
with each cluster carrying its matched Polymarket markets (Jaccard on
keywords) and the strongest surge z-score across its items.

## Market signal ("edges")

Each Polymarket sweep also writes a per-event price snapshot
(`event_slug`, `favorite_question`, `favorite_price`, `volume`) into the
`market_prices` table. `price_velocity.py` computes the 24-hour percentage
change in the favorite market's price; `edge.py` enriches every matched
market with that velocity.

The **mispricing score** is `surge_signal − 10 × min|velocity_pct|`. With
the default `CULTURE_EDGE_VELOCITY_PENALTY=10`, a 30% market move cancels a
z=3 surge signal. Edges with `mispricing > 0` are surfaced and ranked.

UI shows three things per edge: surge z-score, mispricing score, and per-market
price + 24h velocity (green when |Δ|<5%, amber when larger). Velocities require
≥2 snapshots — first 24h of operation will show "price history building".

`market_prices` is pruned to 30 days each cycle.

### Order-book depth

Each price snapshot also captures `best_bid` / `best_ask` / `mid_price` /
`spread_bps`. Velocity is computed against **mid-price** (more honest than
last-trade, which can lag). Markets wider than `CULTURE_MAX_SPREAD_BPS`
(default 500 = 5%) are dropped from the edges panel — wide spread = thin
liquidity = unreliable price signal.

### Signal validation (backtest)

`backtest.py` walks every historical `surge_alerts` entry where the source
is `culture_markets`, extracts the event slug, and compares the price at
the alert time vs. the price `CULTURE_BACKTEST_WINDOW_HOURS` later
(default 24h). Realised |Δ| ≥ `CULTURE_BACKTEST_HIT_THRESHOLD` (default 5%)
counts as a hit, half-that-or-more as a weak hit, otherwise a miss.

Returned via `GET /api/backtest?days=30` and rendered in the "Signal
validation" panel under the edges. This is a backwards-looking sanity check
— it answers "when our surge alarm fired on a culture market, did the
price actually move?".

Note: topic-level surges (Reddit, TikTok, etc.) are *not* yet backtested
because we don't persist which topic clusters existed at past timestamps.
That requires snapshotting cluster state at each scrape, deferred to a
later iteration.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/digest` | Latest LLM digest (markdown + token counts + model). |
| `POST /api/digest/refresh` | Regenerate on demand. |
| `GET /api/index` | Composite + per-section scores, calibration. |
| `GET /api/index/history?hours=72` | Time series of overall score (max 30d). |
| `GET /api/surges?limit=20` | Items with positive z-score vs trailing 7-day baseline, with their full trajectory. |
| `GET /api/topics?limit=20` | Cross-source topic clusters, each with matched markets + surge signal. |
| `GET /api/edges?limit=20` | Topics that have matched markets AND a surging signal — the dashboard's "market signal" panel reads this. Includes per-market spread, velocity, and downsampled price trajectory for the inline sparkline. |
| `GET /api/backtest?days=30` | Hit/weak/miss rate of historical market-source surges plus the per-alert breakdown. |
| `GET /api/section/{section}` | Top items in a section, deduped across sources. Pass `?dedup_results=false` to see the raw rows. |
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

# app/scrapers/ — Social media scrapers

Source-of-truth ingestion. Each scraper fetches recent posts matching a keyword
list and returns a list of `RawPost` rows ready for the database.

## Files in this directory

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `base.py` | `BaseScraper` ABC — every scraper implements `fetch(keywords, limit)` and `is_available()`. Keeps the pipeline polymorphic over future sources. |
| `twitter.py` | `TwitterScraper` — uses `tweepy` against the X API v2. Reads `TWITTER_BEARER_TOKEN` and `TWITTER_MONTHLY_QUOTA` from settings; tracks usage so it doesn't blow the quota mid-month. Gracefully `is_available() == False` if no token. |
| `truthsocial.py` | `TruthSocialScraper` — wraps the TruthSocial v1 API (Mastodon-style). Strips HTML out of post bodies. Reads `TRUTHSOCIAL_USERNAME` / `TRUTHSOCIAL_PASSWORD` / `TRUTHSOCIAL_ACCESS_TOKEN`. |

Both scrapers are called from `app/scheduler.py` inside `run_pipeline()`. New
scrapers (Reddit, Substack, etc.) should subclass `BaseScraper` and be wired
into the pipeline there.

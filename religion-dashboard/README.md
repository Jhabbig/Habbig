# Religion & Cults Tracker

Tracks the global religious landscape, a curated watchlist of new religious
movements (NRMs) and notable historical cults, religious-freedom
designations, and live signal from Polymarket religion-tagged markets and
public news RSS feeds.

Listens on `:7062`. Subdomain: `religion.narve.ai` (registered in
`gateway/config.json`).

## What it tracks

| Section | Source | Cadence |
| --- | --- | --- |
| World religions adherent counts | Pew Research, baseline 2020 estimates | curated — bumped on new edition |
| Sub-tradition breakdowns | Pew + World Religion Database | curated |
| Religious-freedom designations | USCIRF Annual Report 2024 (CPC / SWL / EPC) | curated — bumped each annual report |
| Cults / NRMs watchlist | Britannica, ICSA, FBI case files, court records | curated |
| Markets | Polymarket Gamma — `religion`, `pope`, `vatican`, `catholic`, `papacy` tags | live, 5-min cache |
| News | BBC Religion & Ethics, RNS, AP Religion, Vatican News (RSS) | live, 10-min cache |

## Inclusion criteria

The cult / NRM watchlist intentionally lists only groups with substantial
public documentation in academic, journalistic, or court sources. The
"risk" field reflects publicly documented harm — violence, mass-suicide,
or criminal convictions of leadership. New entries should cite at least
two independent public sources.

The dashboard is descriptive, not theological. It does not classify
mainstream religions as cults; the watchlist is restricted to groups
that meet established sociology-of-religion criteria for "new religious
movement" plus documented external scrutiny.

## Run locally

```bash
pip install -r requirements.txt
python3 server.py
# → http://localhost:7062
```

## Endpoints

- `GET /api/health` — liveness
- `GET /api/summary` — page-load totals (no live calls)
- `GET /api/religions` — world religions adherent counts + sub-traditions
- `GET /api/cults` — curated NRM / cult watchlist (filterable: `?status=`, `?risk=`)
- `GET /api/freedom` — USCIRF 2024 designations (CPC / SWL / EPC)
- `GET /api/markets` — Polymarket religion-tagged markets (live)
- `GET /api/news` — aggregated religion news (live)

## Container

```bash
docker build -t religion-dashboard .
docker run --rm -p 7062:7062 religion-dashboard
```

## Notes

- Static datasets live in `religion_data.py`. Update them by hand when a
  new edition of the source report ships — these are reference numbers,
  not a live feed.
- Polymarket tag set bleeds into politics and crypto; the fetcher applies
  a strict religion-keyword filter on top of the tag query.
- News RSS feeds are noisy. Items are deduplicated by link and capped at
  60 most-recent before serving.

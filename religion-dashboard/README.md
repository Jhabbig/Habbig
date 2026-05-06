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
| Full registry (100 traditions) | Pew + WRD + ARDA + Britannica + official censuses | curated |
| Religious leaders + actuarial | Public bios + SSA 2022 period life table | curated; actuarial computed at request |
| Country religion composition | Pew "Religious Composition by Country" (2010-2050 series) | curated — top 30 by population |
| Religious calendar (2026) | Ecclesiastical calendars + Pew calendar reference | curated annually |
| Religious-freedom designations | USCIRF Annual Report 2024 (CPC / SWL / EPC) | curated — bumped each annual report |
| Cults / NRMs watchlist | Britannica, ICSA, FBI case files, court records | curated; 4-axis risk score |
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
- `GET /api/religions` — world religions adherent counts + sub-traditions (Pew top-8)
- `GET /api/religions-full` — 100-tradition registry (filterable: `?family=`, `?q=`)
- `GET /api/leaders` — religious leaders with life-table actuarial (`?ref=YYYY-MM-DD` overrides today)
- `GET /api/countries` — country religion composition + cross-country rollup (`?religion=` filter)
- `GET /api/calendar` — 2026 religious calendar (`?upcoming=1&days=N` for forward window)
- `GET /api/cults` — curated NRM / cult watchlist with 4-axis risk score (filterable: `?status=`, `?risk=`)
- `GET /api/freedom` — USCIRF 2024 designations (CPC / SWL / EPC)
- `GET /api/markets` — Polymarket religion-tagged markets (live)
- `GET /api/news` — aggregated religion news (live)

## Cult risk scoring

Each watchlist entry has a 4-axis sub-score (0-10), composited into a
`risk_score` (mean) and bucket label (`extreme`/`high`/`moderate`/`low`).
Axes follow the ICSA group-assessment criteria + cult-studies literature
(Singer's "Cults in Our Midst", Lalich's "Bounded Choice"):

- `financial_opacity` — public filings absent, no audits, opaque revenue
- `leadership_risk` — single founder, no successor, charismatic dependency
- `isolation` — closed compound, severance of ties, restricted contact
- `criminal_disclosure` — convictions, ongoing investigations, abuse pattern

## Leader actuarial model

`/api/leaders` walks the SSA 2022 period life table forward in monthly
steps starting from each leader's age on the reference date (defaults to
today; override with `?ref=YYYY-MM-DD`). Returns P(alive 1y/5y/10y) and
P(dies in 1y) per leader, sorted highest mortality first — most relevant
for prediction-market pricing. SSA is a US-population baseline; religious
leaders typically have above-average longevity, so treat as a conservative
prior and adjust upward when domain priors warrant.

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

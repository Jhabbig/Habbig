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
| Religious leaders + actuarial | Public bios + SSA 2022 + 32-leader historical calibration | curated; actuarial computed at request |
| College of Cardinals + papabile | Vatican Press Office + College of Cardinals Report + Vaticanist press | curated; bumped at each consistory |
| Country religion composition | Pew "Religious Composition by Country" (2010-2050 series) | curated — 159 countries, ~98% of world population |
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
- `GET /api/leaders` — religious leaders with life-table actuarial (`?ref=YYYY-MM-DD` overrides today). Returns both raw-SSA and religious-office-adjusted probabilities.
- `GET /api/historical-leaders` — 32-leader cohort used to calibrate the religious-office hazard ratio (0.85)
- `GET /api/conclave` — College of Cardinals sample, papabile priors, conclave rules + college aggregates (filters: `?region=`, `?wing=`, `?electors=1`, `?papabile=1`)
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

## Conclave model

`/api/conclave` powers the suite's flagship analytical feature. When the
Holy See becomes vacant, Polymarket conclave markets get massive volume
and most retail bettors guess based on flag affinity — there is genuine
edge.

The endpoint returns three things:

1. **Cardinal sample** (~80 entries) — the most influential and most-
   discussed cardinals. Each has: country, region, age, elector status
   (under 80), appointed-by (which pope), wing (progressive / moderate /
   conservative / traditional), papabile tier (0 = not discussed,
   3 = top-tier favourite), and a one-line summary.

2. **Papabile priors** (~15 entries) — names, rationale, and a prior
   probability reflecting Vaticanist + bookmaker consensus. Priors sum
   to ~59%; the residual ~41% is the "field" (someone outside the
   shortlist — historically a common outcome).

3. **College aggregates** — full-college breakdown beyond the sample:
   252 cardinals total, ~135 electors, ~80% created by Francis. Used
   to sanity-check coalition arithmetic.

Sources: Vatican Press Office bollettino, the College of Cardinals
Report (cardinalsreport.com), Vaticanist English-language press
(Allen/Crux, Magister, La Croix, The Pillar). Wing assignments are
journalistic shorthand and contested. Bump at each consistory.

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

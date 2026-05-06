# State of Love Dashboard (v0 — wireframe)

A global "State of Love" dashboard that tracks marriage, divorce, sexual
activity, and connection-quality signals as a happiness proxy. This v0 is
**wireframe-only** — `static/index.html` is mocked with placeholder numbers
so we can iterate on layout and metric selection before wiring a backend.

Planned port: `7060`. Planned subdomain: `love.narve.ai`.

## Plan

Three layers, mirroring the climate/world-state dashboards:

1. **Composite Love Index (0–100)** per country, plus a global aggregate.
2. **Four subscores** that the index is built from — each transparent and
   drillable.
3. **Insight engine** that surfaces movers, divergences, and event-driven
   changes (war, recession, legalization) so the dashboard is *actionable
   reading*, not just a wall of numbers.

### Composite weighting (proposal — to revisit)

| Subscore | Weight | What it captures |
|---|---|---|
| Connection | 35% | Loneliness, social-support, satisfaction with relationships |
| Commitment | 25% | Marriage rate, 1 − (divorce / marriage), median union duration |
| Family stability | 20% | Births in stable unions, single-parent share, age at first union |
| Romantic activity | 20% | Dating-app penetration, "love"/"date" search interest, condom imports per capita |

Each subscore is z-scored within continent, then min-maxed to 0–100 globally.
Continent-relative z avoids penalizing regions where the cultural baseline is
different (e.g. cohabitation-heavy Nordics vs. marriage-heavy South Asia).

## Data-source audit

Three tiers, ranked by accuracy. Every number on the dashboard will carry a
visible tier badge so users can judge it.

### Tier A — registry / authoritative (slow, accurate)

| Metric | Source | Format | License | Coverage | Lag |
|---|---|---|---|---|---|
| Crude marriage rate | UN DESA Demographic Yearbook | XLSX | UN open, attribution | ~200 | 1–3 yrs |
| Crude divorce rate | UN DESA Demographic Yearbook | XLSX | UN open, attribution | ~150 | 1–3 yrs |
| Marriage / divorce (EU) | Eurostat `demo_nind`, `demo_ndivind` | JSON API | CC-BY 4.0 | EU + EFTA | ~1 yr |
| Family indicators | OECD Family Database | XLSX | OECD open, attribution | OECD ~38 | 1–2 yrs |
| Births outside marriage | OECD Family Database | XLSX | OECD open | OECD | 1–2 yrs |
| Age at first marriage, fertility | UN WPP | CSV | CC-BY 3.0 IGO | global | 1–2 yrs |
| Population / age structure | World Bank WDI | JSON API | CC-BY 4.0 | global | 1 yr |
| HIV / STI incidence | WHO Global Health Observatory | OData / CSV | check per indicator | ~190 | 1–2 yrs |
| Condom trade flows | UN Comtrade (HS 401410) | JSON API | CC-BY-IGO | ~200 | 1–2 yrs |

### Tier B — survey (broad, biased)

| Metric | Source | Format | License | Coverage | Lag |
|---|---|---|---|---|---|
| Life satisfaction, social support | World Happiness Report (Gallup) | CSV in appendix | aggregates open; raw Gallup paid | 150 | annual |
| Family / relationship values | World Values Survey | SPSS / CSV | non-commercial, registration | ~100 | wave (~5 yr) |
| Loneliness | Meta-Gallup *State of Social Connections* | PDF / CSV | report-level open | 142 | 2024+ |
| Sexual behavior | NATSAL (UK), GSS (US), national equivalents | per-country | mostly open | sparse | irregular |

### Tier C — proxy (live, biased)

| Metric | Source | Format | License | Coverage | Lag |
|---|---|---|---|---|---|
| Search interest ("divorce", "marry", "tinder", "love") | Google Trends | unofficial API | display-only, no commercial redistribution | global | live |
| Dating-app penetration | Match Group / Bumble investor decks; SimilarWeb / data.ai (paid) | varies | proprietary; small public summaries | top markets | quarterly |
| App-store rankings (dating category) | iTunes / Google Play public charts | scraped | ToS-sensitive | global | live |
| Public sentiment ("love", "breakup") | Reddit Pushshift / X (paid) | varies | ToS-sensitive | global | live |

### Quick-start data set (week-1 build)

To get a defensible v1 fast, start with the intersection of *free + global +
machine-readable*:

- World Bank WDI — population, fertility, life expectancy (baseline normalizers)
- UN DESA marriage / divorce — Tier A backbone
- World Happiness Report appendix CSV — Connection subscore
- Eurostat — high-resolution Europe layer
- Google Trends — single live signal for the Activity subscore

That's enough to render every card in the wireframe with real numbers; Tier B
and the rest of Tier C are upgrades, not blockers.

## Insight engine (the "actionable" part)

Each insight is a small rule that runs over the latest snapshot and emits a
card if it fires. Examples:

- **Mover** — country's Love Index changed ≥ 5 pts YoY
- **Divergence** — Activity subscore up while Commitment subscore down (or vice versa)
- **Event overlay** — index inflection within 12 months of a known event
  (legalization, war, pandemic, recession) — flag for narrative
- **Outlier** — country sits >2σ from its continent on any subscore
- **Pair compare** — "X looks like Y did 10 years ago"

Insights are stored with provenance (which inputs, which window) so a reader
can click through to the raw series.

## Layout (see `static/index.html`)

- **Header** — global Love Index + WoW / YoY deltas, last-updated stamp
- **Subscore strip** — 4 cards (Connection / Commitment / Stability / Activity)
- **World map** — choropleth, color = index, click = country drill-down
- **Top movers** — up & down lists
- **Insight feed** — auto-generated cards from the rules above
- **Country detail panel** — index timeseries + subscore breakdown + raw inputs
- **Data-quality footer** — tier mix, coverage %, freshness per source

## Open questions (please push back)

1. Is "Love" the right umbrella, or should the public-facing name be
   "Connection" / "Relationships"? (Affects which metrics feel on-brand.)
2. Country-level only, or also regional / city where data allows?
3. Do we publish the raw Love Index, or only the four subscores? (Composite
   is more clickable but easier to argue with.)
4. How heavily do we lean on Tier C? Cheap and live, but the methodology is
   harder to defend.

## Run locally (wireframe only)

```bash
cd love-dashboard
python3 -m http.server 7060 --directory static
# → http://localhost:7060
```

A real `server.py` + `Dockerfile` come once the metric set is locked.

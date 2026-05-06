# State of Love Dashboard (v2)

A global "State of Love" dashboard that tracks marriage, divorce, sexual
activity, and connection-quality signals as a happiness proxy.

**v2 ships:**

- Live data for **3 of 4 subscores** — Connection (WHR social-support),
  Partnership (Eurostat marriage rate), Stability (Eurostat divorce rate +
  World Bank adolescent fertility). Activity remains Tier-C / v1.1.
- **Insight engine** with 4 rule types — peer-leader, outlier, divergence,
  and coverage-gap.
- **Custom weights** — every subscore weight is adjustable via URL params
  (`?w_connection=0.5&w_partnership=0.3&...`); the API recomputes the
  composite live without re-fetching.
- **Country drill-down** — `/api/country/<iso3>` returns the full subscore
  breakdown with peer-comparison (mean and delta within the income tier)
  and raw indicators with units.
- **Frontend product** — search, sortable ranking table for every country,
  D3 + topojson world choropleth, drill-down modal, weight sliders that
  recompute everything live, and a real-time insight feed.

Port: `7060`. Planned subdomain: `love.narve.ai`.

## Plan

Three layers, mirroring the climate/world-state dashboards:

1. **Composite Love Index (0–100)** per country, plus a global aggregate.
2. **Four subscores** that the index is built from — each transparent and
   drillable.
3. **Insight engine** that surfaces movers, divergences, and event-driven
   changes (war, recession, legalization) so the dashboard is *actionable
   reading*, not just a wall of numbers.

## Methodology

### What we measure

The Love Index is a **population-level prevalence-and-quality measure of close
human connection** — how many people in a country have meaningful relationships,
and how good those relationships are. It is *not* an intensity score (we can't
measure how much one couple loves each other), and it is not a values judgement
on family structure (cohabitation and marriage count equally as "partnership").

Falsifiable claim: a country scoring 80 should, on average, have lonelier
people, fewer stable unions, and lower relationship satisfaction than a country
scoring 40. If we can't show that across multiple cuts of the data, the
methodology is wrong.

### Subscores

| Subscore | Weight | Indicators | Tier |
|---|---|---|---|
| **Connection** | 35% | loneliness rate (↓good); social support (↑good); relationship satisfaction (where measured) | B |
| **Partnership** | 30% | partnership rate = % adults in marriage OR cohabiting union (↑good, capped at 80th pctile); median union duration at dissolution (↑good) | A |
| **Stability** | 25% | age at first union (U-shape penalty); separation rate per 1,000 existing unions (↓good) | A |
| **Activity** | 10% | dating-app penetration; Google Trends "love"/"date" basket | C — indicative only |

Things deliberately *not* in the index, with reasons:

- **Crude marriage rate** — replaced by partnership rate. Sweden marries less but cohabits more; outcome is the same.
- **Births outside marriage** — values-laden and doesn't track stability.
- **Divorce rate per population** — replaced by separation per 1,000 existing unions; population denominator gets confounded by falling partnership rates.
- **Condom imports** — too noisy as a romantic-activity proxy.

### Direction of each metric

| Metric | Direction | Reason |
|---|---|---|
| Loneliness rate | ↓ better | direct measure of disconnection |
| Social support | ↑ better | direct measure of connection |
| Partnership rate | ↑ better, **capped** at 80th pctile | runaway-high rates can reflect coercion or absence of single-life options, not flourishing |
| Median union duration | ↑ better | longer unions ≈ more stability |
| Age at first union | U-shape | very young often forced; very late correlates with delayed family formation and loneliness |
| Separation per 1,000 unions | ↓ better | cleaner denominator than crude divorce rate |
| Dating-app penetration | ↑ indicative | high engagement = active romantic market — but also correlates with loneliness; flagged as ambiguous |
| Search interest (love / date) | ↑ indicative | same caveat |

### Normalization

1. Within each subscore, convert each raw indicator to a **percentile rank
   within income tier** (World Bank low / lower-mid / upper-mid / high).
   Income predicts marriage / divorce / loneliness patterns more cleanly than
   geography, and percentile rank is robust to the skewed distributions these
   series often have.
2. Average the indicators within a subscore (equal weight) → subscore
   percentile (0–100).
3. Composite Love Index = weighted average of subscore percentiles using the
   table above.

We do **not** z-score then min-max as in the previous draft. Z-scoring
reshapes skewed distributions, and min-max means the worst country always
sits at 0 even if the global state improves. Percentile rank within income
tier is harder to game and easier to explain ("compared to peers at similar
income").

### Missing-data policy

- **Subscore present**: ≥1 of its indicators must be measured. If only one of
  two, the subscore carries a "low-confidence" flag.
- **Country ranked**: ≥2 of the 3 Tier-A/B subscores must be present.
  Activity alone is never enough.
- **No imputation** of missing values. Missing subscores drop out of the
  weighted average and remaining weights renormalize. The country detail
  panel shows which subscores were used.

### Sensitivity analysis (planned, before any public ranking)

Publish a table showing how the top 20 changes under:

- Weights perturbed ±10 percentage points
- Each subscore dropped one at a time (leave-one-out)
- Z-score-then-min-max swapped in for percentile rank
- Income-tier grouping swapped for continental grouping

Countries that shuffle wildly across these are flagged **unstable** in the UI
rather than given a confident rank.

### Contestable decisions (push back on these before backend work)

The five calls above I'm least certain about:

1. **Activity at 10%, badged "indicative only".** Alt: drop it from the index entirely and show it as a side-panel "Activity Watch". *(My pick: keep at 10% — you flagged it as interesting and the visible badge handles the credibility issue.)*
2. **Partnership rate, not marriage rate.** Alt: marriage rate alone is cleaner Tier-A coverage; cohabitation data is patchy outside Europe. *(My pick: partnership; we lose ~20 countries of Tier-A coverage but the methodology is consistent across the panel.)*
3. **Income-tier normalization, not continent.** Alt: continent is more recognizable to readers. *(My pick: income tier; the framing "vs peers at similar income" is more honest.)*
4. **Drop births-outside-marriage entirely.** Alt: keep as a stability proxy in countries with clear cultural meaning. *(My pick: drop; it's a values trap.)*
5. **Cap partnership rate at 80th percentile.** Alt: don't cap. *(My pick: cap; rewarding coercion-driven high rates would discredit the index.)*

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

Methodology contestables are now in the *Contestable decisions* block above.
Two open product questions remain:

1. Is "Love" the right public-facing umbrella, or should it be "Connection"
   or "Relationships"? Affects tone but not metrics.
2. Country-level only, or also regional / city where data allows
   (e.g. Eurostat NUTS-2 has good union-status coverage)?

## Run locally

```bash
cd love-dashboard
pip install -r requirements.txt
python3 server.py
# → http://localhost:7060
```

Or via Docker:

```bash
docker build -t love-dashboard .
docker run --rm -p 7060:7060 love-dashboard
```

## Endpoints

All read-only; CORS open. Every endpoint that returns scored countries
accepts `?w_connection=<0..1>&w_partnership=<0..1>&w_stability=<0..1>&w_activity=<0..1>`
to recompute with custom weights (renormalized to sum to 1; partial sets
are merged with defaults).

- `GET /api/health` — liveness
- `GET /api/summary` — single-page payload: global index, subscore averages, coverage, top/bottom 10, weights, weights_customized flag
- `GET /api/index` — every ranked country with subscores, raw indicators, and which subscores were used
- `GET /api/countries` — full ranked + unranked list (unranked rows include `unranked_reason`)
- `GET /api/country/<iso3>` — country's record + `peer_compare` (income-tier mean and Δ for each subscore)
- `GET /api/insights` — insight cards generated by `insights.py` rules
- `GET /api/sources` — methodology constants, current subscore coverage, per-feed in_use status

## Tests

The methodology math is covered by an offline test that mocks the fetchers,
so it runs without internet:

```bash
python3 test_methodology.py
```

Covers: weights sum to 1.0, percentile rank within income tier (top/bottom,
inversion, cap, cross-tier independence), missing-data policy (≥2 of 3
Tier-A/B subscores required), and weight renormalization when a subscore is
missing.

## Roadmap (v3)

- **Activity subscore** — Google Trends "love" / "date" basket + dating-app
  penetration from SimilarWeb / public investor decks.
- **Global Partnership coverage** — UN DESA Demographic Yearbook XLSX
  scraper for marriage / divorce data outside Europe.
- **Time series** — historical snapshots so the insight engine can produce
  Mover and Event-overlay narratives (legalization, recession, pandemic).
- **Sensitivity analysis page** — surface the leave-one-out and ±10% weight
  perturbations described in the methodology, flag unstable rankings.
- **Loneliness layer** — Meta-Gallup *State of Social Connections* once we
  agree on a parser for their PDF / report-level CSV.

# Voter Dashboard — v2 Roadmap

v1 ships a single national mood index plus the indicators driving it. v2 is
about making the dashboard *answer questions* instead of just *report
numbers*: who is feeling what, where, and what does that imply for the next
election cycle.

## Guiding principles

- **Stay keyless.** Every v1 source works without an API key. Hold that
  line — anything that *requires* a paid endpoint goes behind a feature
  flag, not the front page.
- **Cuts beat composites.** A single national 0–100 number is a useful
  starting point but a poor end state. v2 should let users slice by party,
  state, age band and income quintile.
- **Make claims, not displays.** A card that says "real wages are up 0.7%
  YoY" is a fact. A card that says "this is the strongest real-wage growth
  for the bottom income quintile since 2019" is a claim. Aim for claims.

## v2 features, ranked by leverage

### 1. Partisan sentiment — needs a source decision

**Status: data sourcing problem.** My original write-up claimed UMich
partisan splits are on FRED — they aren't. UMich publishes them in their
quarterly Table 32 as PDFs/Excel files in the Surveys of Consumers data
archive. Three viable paths, in increasing order of effort:

1. **Scrape UMich's data archive** — `data.sca.isr.umich.edu` exposes a
   table query UI; the underlying form posts return CSV. Brittle but free.
2. **Re-create the partisan signal from approval polls** — aggregate
   538-style approval polls (the archived CSV is still on GitHub) with the
   pollster's stated respondent partisanship, smooth daily. This is *not*
   the same metric as UMich sentiment but is arguably more politically
   relevant.
3. **Subscribe to an aggregator** — Morning Consult, Civiqs and Pew all
   sell partisan economic-sentiment feeds. Out of scope for the
   "free dashboards" mandate.

Recommend pursuing (2) — covered by Phase 2 item *Approval ingestion*
below — and treating partisan UMich as a stretch goal.

### 2. State-level mood map &middot; **SHIPPED v1.1**

50 states + DC unemployment from FRED's `<STATE>UR` family, plus a
percentile-vs-own-20y-history "stress score" per state. Surfaced as:
- a 2024 swing-state strip (PA, MI, WI, AZ, GA, NV, NC),
- five most-stressed and five least-stressed states ranked by stress,
- `/api/states` endpoint serving the full panel.

**Next**: actual choropleth (tile-grid or SVG paths), state-level CPI for
the dozen FRED metro series, and state-level gas via EIA HTML scrape.

National misery + sentiment hides huge variance. Surface it:
- BLS Local Area Unemployment Statistics has state-level UNRATE
  (`LAUST<FIPS>0000000000003` or via FRED state-level series like
  `CAUR`, `TXUR`, `FLUR`).
- BEA per-capita income via FRED state series.
- AAA / EIA gasoline by state (EIA has weekly state-level retail prices,
  keyless via FRED's `GASREG_<STATE>` family or scrape the EIA HTML).

Render: choropleth of a state mood index using the same equal-weighted
formula. Hover for the per-state mood card. Add a "swing-state mood" strip
at the top for the seven 2024 swing states.

### 3. Demographic cuts (age × income × education)

Most pollsters publish sentiment cuts; the BLS publishes earnings cuts.
For v2 the right move is:
- BLS quarterly real earnings by income quintile (`LES1252881500` and
  siblings).
- Census ASEC poverty rate and median household income by age band.
- Render as a small-multiples grid: 4 sparklines per indicator, one per
  quintile.

This is the single biggest "claim, not display" win — being able to say
"real wages are up for the top quintile and down for the bottom" instead
of "real wages are up 0.4%".

### 4. Approval ingestion &middot; **SHIPPED v1.3 (historical)**

Pulls 538's archived `president_approval_polls.csv` from the GitHub
mirror, auto-detects the most-recent incumbent, computes a 4-week
sample-size-weighted rolling net, and surfaces a 52-week sparkline. When
the latest poll is > 60 days old the card flags itself as "historical".

**Still to do**:
- Splice a live source past the 538 freeze date — Polymarket
  end-of-year approval markets (which we already fetch) can give an
  implied current approval; RCP scrape or Silver Bulletin API otherwise.
- "Right track / wrong track" parallel ingestion.
- Pollster-by-pollster scorecard (rolling Brier vs the eventual result).

### 4b. Vibecession quantifier &middot; **SHIPPED v1.3**

`gap(t) = sentiment_percentile(t) − fundamentals_percentile(t)`, both
computed monthly against the prior 20 years. Fundamentals composite is
the mean of three 0-1 sub-scores: 1 − unemployment percentile,
1 − CPI-YoY percentile, sigmoid of real-wage YoY.

Rendered as a card next to approval with a big signed gap number, the
sentiment-vs-fundamentals scores side-by-side, a rank-among-history line
("rank 24 of 384 months"), and a sparkline of the gap with red/green
fills for negative/positive territory. The full method is exposed in
the JSON payload — no black box.

This is the dashboard's most novel single number — nobody else
publishes the gap as a quantitative series. v2 should add:
- Multi-component decomposition (which fundamental is most out of step
  with sentiment).
- "Vibe regime" classification: persistent positive gap (e.g. 2017-19)
  vs persistent negative gap (e.g. 2022-24).
- The same gap by partisan / demographic cut once those splits land.

### 5. Election-cycle context &middot; **SHIPPED v1.2**

OLS of incumbent-party House seat change on UMich consumer sentiment in
April of each midterm 1978-2022 (n=12). Surfaced as a flagship banner
under the mood index showing the implied seat change with a 90%
prediction interval, plus an inline scatter plot of every historical
cycle with regression line + prediction band.

R² is genuinely low (~0.09 in current data) and the dashboard publishes
it openly — that honesty is part of the value. Political narratives
overstate how predictable midterms are from mood alone.

**Next**: extend to presidential popular-vote share, replicate as a
multi-variable model (mood + real wages YoY + incumbent-tenure
dummies), publish a backtest accuracy report.

### 6. "What changed" feed

A reverse-chronological feed of every notable indicator move:
"CPI YoY printed 2.7% (down from 3.1% prior month)", "Initial jobless
claims jumped to 248k vs 220k 4-wk avg", etc. Auto-populated from the
existing biggest-movers logic, but persisted across reloads. Useful for
people who check the dashboard daily.

Implementation: append to a SQLite log on every successful fetch when a
series prints; render the last 50 entries.

### 7. Compare-to-history overlay

On every card, a button that toggles "show this indicator's path during
the last comparable period." For inflation, that's 1979–82. For a
mortgage card, that's 2006–08. Useful for the "have we ever been here
before" question.

### 8. Polymarket: model-edge scoring

Mirror the climate dashboard's pattern: parse the question of each
politics market and attach a model probability where we can.

- For approval markets ("Will X have ≥50% approval at year end?") —
  drift forward from current Polymarket-implied or aggregator-implied
  approval using the historical residual std.
- For "right track / wrong track" markets — use the misery-index and
  sentiment percentile as the model probability.
- Display an *edge in pp* column like the climate page does.

This is the highest-effort item and depends on (4) landing first.

### 9. Push → poll → realtime

v1 is pull-on-page-load with a 5-min auto-refresh. v2 should:
- Move FRED fetches to a background poller that updates a shared cache
  (same pattern as `crypto-dashboard`).
- Add `Last-Modified` / `If-None-Match` headers to FRED requests to
  short-circuit when nothing has changed.
- Optionally an SSE endpoint for the front page so we can push updates
  without the 5-min polling loop. This is mostly cosmetic — FRED data
  doesn't move minute to minute.

### 10. Composite-index transparency

Right now the mood index is a black-box "55 / 100." v2 should:
- Add `/api/mood/explain` returning a paragraph: "Sentiment is in the
  62nd percentile vs the last 20y, jobs in the 88th, inflation in the
  31st, …"
- Click any sub-score bar in the banner to drill into that component's
  full series.

## Things explicitly out of scope for v2

- **Custom polls.** The point is to consolidate, not replace, existing
  polling.
- **Per-state Polymarket coverage.** Polymarket doesn't have meaningful
  state-level political markets.
- **Live election forecasting.** The midterm-dashboard exists for that.
  v2 should *link* to it, not duplicate it.

## Suggested execution order

A weekend each:

1. Partisan + demographic UMich splits (item 1) — pure data add, biggest
   feel-improvement per hour spent.
2. Election-cycle regression (item 5) — turns the dashboard into a
   product, not a display.
3. State-level map (item 2) — visually impressive, well-bounded scope.
4. Approval ingestion (item 4) — unlocks the model-edge story.
5. Polymarket model-edge (item 8) — wraps it up.

Items 6, 7, 9, 10 are quality-of-life and can land any time.

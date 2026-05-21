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

### 1. Partisan sentiment &middot; **SHIPPED v1.5 (quarterly snapshot)**

UMich publishes consumer sentiment by respondent partisanship in
quarterly Table 32 PDFs, not on FRED. v1.5 ships a hand-transcribed
historical series back to 2017 in `PARTISAN_UMICH_HISTORY` (in
`server.py`) and renders a partisan-gap card with R / D / I pills, a
dual-line R-vs-D chart, and historical extremes.

The data is honest about being quarterly and manually maintained — the
file has a comment about how to refresh from
`data.sca.isr.umich.edu` when a new Table 32 is published.

**v2 follow-ups**:
- Automate the refresh — UMich's data-archive form posts return CSV; a
  small scraper would keep this current.
- Add the partisan-gap to the methodology page's backtest section: how
  does the gap correlate with the eventual election outcome?
- Add the same R/D/I dual-line view for the *vibecession* gap once we
  can compute it by party.

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

### 3. Demographic cuts &middot; **PARTIALLY SHIPPED v1.6**

v1.6 ships **real wages by income decile/quartile** — `LEU0252881*`
series from BLS via FRED for the bottom decile, P25, median, P75, and
top decile. Surfaced as a 5-tile panel below the cards with a plain-English
claim ("Wage growth is uneven — top decile +4.0% YoY vs bottom +0.5%,
spread 3.5 pp").

**Still to do**: age-band cuts, education cuts, and a real-vs-nominal
toggle on each tile.

### 3a. (original heading) Age × income × education

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

### 4. Approval ingestion &middot; **SHIPPED v1.3-1.4**

v1.3 — Pulls 538's archived `president_approval_polls.csv` from the
GitHub mirror, auto-detects the most-recent incumbent, computes a 4-week
sample-size-weighted rolling net, surfaces a 52-week sparkline. When the
latest poll is > 60 days old the card flags itself as "historical".

v1.4 — **Live splice via Polymarket.** For each end-year with ≥2
"approval ≥ X%" markets, we treat the implied prices as a discrete CDF
and interpolate to find the threshold where P(approval ≥ X) = 0.5 — the
implied median approval. Surfaced as forward dots on the approval
sparkline + an annotation in the context line ("Polymarket-implied EOY
2026: ~44% approve").

**Still to do**:
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

### 4c. Right-direction / wrong-direction &middot; **SHIPPED v1.5 (snapshot)**

A second hand-curated series (`RIGHT_TRACK_HISTORY`) covering 2020-2025
quarterly. Rendered as a card next to partisan sentiment showing the
net (right − wrong) headline + sparkline with zero baseline. Sources
aggregated from RCP, Reuters/Ipsos, CBS, NBC, AP-NORC monthly averages.

**v2**: Same automation as partisan — scrape RCP's "Direction of
Country" page to keep current.

### 4d. Pollster scorecard &middot; **SHIPPED v1.5**

Pulls FiveThirtyEight's archived `pollster-ratings.csv` from the GitHub
mirror and ranks pollsters by Predictive Plus-Minus (lower = more
accurate). Renders top-10 most-accurate and bottom-10 least-accurate
side-by-side with 538 grade pills. Same staleness considerations as the
approval CSV.

**v2**: Compute our own pollster scorecard once we have enough cycles
of recent polls (the 538 ratings stop in mid-2024).

### Global mood &middot; **SHIPPED v1.5**

Same mood-composite formula computed for six countries (US, UK,
Germany, France, Canada, Japan) using OECD consumer-confidence
indicators + harmonised unemployment + national CPI, all keyless via
FRED's International section. Rendered as a strip of flag tiles below
the mood banner — directly comparable across countries because every
country's mood is computed identically.

**v2**: Add Brazil, India, Mexico for emerging-market mood. Per-country
methodology pages.

### Persistent snapshot DB &middot; **SHIPPED v1.5**

SQLite DB (`voter_snapshots.sqlite3`) that records every FRED
observation on first sight and logs revisions when a re-fetch returns a
different value for the same date. Surfaced via `/api/revisions` and
the methodology page (recent-revisions table + stats). Path is
overridable via the `VOTER_SNAPSHOT_DB` env var.

This is what makes backtests credible — we can re-run them against
as-known-then snapshots instead of the retroactively-revised history.

**v2**: Re-run the election-cycle backtest using snapshot data instead
of latest FRED values; publish the side-by-side comparison.

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

### 6. "What changed" feed &middot; **SHIPPED v1.6**

SQLite-backed persistent feed of notable indicator moves. `biggest_movers`
auto-logs any 3-month change with |z| ≥ 1, dedup'd by series + value so a
refresh in the same hour doesn't spam the log. Revisions detected by the
snapshot DB are mirrored to the feed too. Surfaced as:

- A "what changed" panel on the main page with the last 30 events,
  good/bad-for-voter color dots, and human-readable timestamps.
- `/api/changes` — JSON feed
- `/api/changes.rss` — RSS 2.0 feed, pasteable into any reader, suitable
  for the daily-digest pipeline below.

**v2 still to do**: email digest. Requires SMTP config + opt-in list +
unsubscribe machinery — out of scope for the keyless-static-deploy
mandate. Recommend layering on top of the RSS feed via a separate
worker that polls /api/changes daily and emails subscribers.

### 6b. Original "what changed" item

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

## Deliberately deprioritized

These came up as candidates during the v1.5–v1.6 push and got
deprioritized for honest reasons:

- **UMich data-archive scraper / RCP HTML scraper.** Form posts and HTML
  scraping are fragile; the partisan and right-track data is already
  surfaced via hand-curated quarterly snapshots that take 5 minutes to
  refresh. The auto-refresh would gain a couple of weeks of latency and
  trade it for a class of silent breakages. Revisit if a maintainer
  can't keep the snapshots fresh manually.

- **Email digest / Twitter/X bot.** Both require credentials we don't
  manage from the dashboard (SMTP / Twitter API). The `/api/changes.rss`
  feed is the right primitive — a small external worker can poll it and
  fan out to email or social. Keeping the credential-bearing layer
  outside the dashboard keeps the dashboard itself fully keyless and
  free.

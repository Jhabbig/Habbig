# The Prediction-Extraction Engine — flagship pitch

*The "main LLM extraction tool" of the narve.ai suite: the two-stage
regex + Claude pipeline in `app/processing/` (`extractor.py` +
`llm_extractor.py`) and the scoring loop wrapped around it.*

---

## One sentence

**The internet is full of people making predictions; this engine turns that
noise into a priced, scored, settled order book of opinions — and tells you
which voices are actually worth money.**

## The problem

Every day X, TruthSocial, Reddit, and a thousand newsletters emit millions of
forward-looking claims — "the Fed cuts in March", "no chance the bill
passes", "BTC 150k by EOY, 70%". Three things make that stream worthless in
raw form:

1. **It's unstructured.** Predictions hide in sarcasm, hedges, threads, and
   multi-clause sentences. Keyword search can't find them; a human can't read
   fast enough.
2. **It's unpriced.** A prediction only matters relative to what the market
   already believes. "Trump wins PA" is worthless at 95¢ and gold at 40¢.
3. **It's unaccountable.** Nobody goes back and checks who was right. Loud
   and correct look identical on a timeline.

## What the engine does

A four-stage pipeline, each stage independently testable (`app/tests/`):

**1. Extract** — the LLM extraction core.
Precise regex patterns run first: explicit "X% chance" phrasing and
directional language, free and instant (`extractor.py`). Whatever the regex
can't parse falls through to a Claude-powered extractor (`llm_extractor.py`)
that reads the post like a human: hedged predictions, indirect speech,
conditionals, mixed languages. Structured output enforced by a Pydantic
schema (`client.messages.parse()` — never a parsing error), a prompt-cached
system prompt (~100% cache hit after the first call), and a per-content-hash
result cache in SQLite, so a viral quote copy-pasted by 400 accounts is
extracted exactly once. If no API key is set, the engine degrades gracefully
to regex-only. Model is swappable via `LLM_EXTRACTOR_MODEL` (Opus for
accuracy, Haiku for ~5× lower cost).

Every extraction is a falsifiable claim: `outcome` (Yes/No), `probability`
(if stated), `category`, verbatim quote, and the extractor's own confidence.

**2. Price** — match to real markets.
Each prediction is matched against live Polymarket *and* Kalshi markets
(Jaccard token overlap, category gating, multi-outcome disambiguation so a
"Trump will win" post can't land on the Harris market). The engine then
computes the EV of buying YES vs NO at the live quote and surfaces the
better side as a `BUY YES` / `BUY NO` signal. Liquidity-aware EV walks the
CLOB order book to show where the edge dies at $100 / $1k / $10k size.

**3. Score** — accountability, automatically.
When a matched market resolves, the resolver marks every tied prediction
correct or incorrect and updates the source's credibility: Bayesian-smoothed
accuracy, decay-weighted by half-life, category-dominance penalties, and
Brier-score calibration for probability-bearing calls. The leaderboard is
the product: a ranked list of who is actually right, per category.

**4. Prove** — a ledger you can audit.
Every signal that clears the EV + credibility filter opens a $1 paper trade,
settled on resolution. The backtest harness replays every resolved
prediction under tunable thresholds and returns ROI, annualised Sharpe, max
drawdown, and the cumulative P&L curve — the "would I trust this with real
money?" interface. No cherry-picking possible: the ledger settles itself.

## Why this wins

- **Honest by construction.** The suite's research arm
  (`financial-matrix-toolkit/`) proved the house view: direction prediction
  in efficient markets barely beats a coin flip. This engine doesn't fight
  that — it hunts the inefficiency that *does* exist: slow-moving prediction
  markets vs fast-moving public information, and the persistent skill
  differences between sources. Every claim is benchmarked against a live
  market price, after the fact, in public.
- **Compounding data moat.** Every settled prediction makes the credibility
  priors sharper. A competitor starting today starts at zero history.
- **Cost-engineered.** Regex-first, LLM-fallback, content-hash caching, and
  prompt caching mean the marginal post costs fractions of a cent, and
  usually nothing.
- **Distribution-ready.** Public JSON API (`/api/v1/signals`, `/sources`,
  `/backtest`, `/arbitrage`) with per-user keys, Telegram alerts and a query
  bot (`/edge @handle`), and a user calibration mode that scores *you* with
  the same Brier methodology.

## Where it sits in the suite

As of this release **every dashboard in the fleet is live** behind the
gateway, and all of them render as tabs of one page — **Narve One**
(`/one`). Truth Research is the flagship tab: the one product that doesn't
just *display* markets but reads the world's opinions and prices them. The
other dashboards are its natural feeder ecosystem — Sports, Midterm, Weather,
Central Bank, AI Race, Religion, World State, Whale, Crypto Trackers each own
a category that the extractor classifies into (`sports`, `politics`,
`crypto`, `geopolitics`, `other`).

**Roadmap hook (not yet built):** category-scoped extraction feeds per
dashboard — e.g. the Midterm tab surfacing the extractor's `politics`
signals next to its polling aggregates, Sports surfacing `sports` signals
next to its +EV finder. One engine, twelve distribution surfaces.

## Numbers that matter (from the shipped backtest harness)

The pitch deliberately quotes **no** invented accuracy or P&L figures. The
product's own backtest tab computes ROI, Sharpe, and drawdown from the
settled ledger — run it and quote *that*. An honest pitch for an honesty
engine.

## Try it

```bash
cd Dashboard-x-truth-research-prediction
cp .env.example .env          # add ANTHROPIC_API_KEY to enable the LLM stage
docker-compose up --build     # http://127.0.0.1:18789
```

Or behind the gateway: `truth.narve.ai`, or the **Truth Research** tab in
Narve One at `/one`. Storefront pitch page: `/preview/truth`.

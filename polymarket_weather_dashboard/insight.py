"""LLM-powered actionable-insight engine.

Takes the full structured context the dashboard already assembles for one
market — question, prices, model probabilities, forecast distribution,
bias + downscaling corrections, intraday running max, per-station skill —
and returns a short, opinionated JSON recommendation: which side to bet,
how confident, the load-bearing facts, the risks, and a suggested limit
price.

Design choices
--------------
* **Prompt caching.** The system prompt carries the dashboard's
  methodology, conventions, station list, and output-schema description.
  None of that changes between requests, so it lives behind a single
  `cache_control: {type: "ephemeral"}` breakpoint. Verify cache hits
  via `usage.cache_read_input_tokens` on the final message.

* **Streaming.** All calls go through `messages.stream()` so the
  frontend can start rendering the headline within ~1s while the rest of
  the JSON continues generating. The endpoint wrapper turns SDK stream
  events into Server-Sent Events.

* **JSON output via `output_config.format`.** The schema lives next to
  the system prompt so changes to either land in the same diff.
  Structured outputs guarantee the response is valid JSON matching the
  schema; consumers parse without try/except plumbing on success.

* **No tool use.** The backend gathers all context up front — keeps
  latency predictable, cost capped, and audit trivial.

* **Default Haiku, opt-in Sonnet.** Haiku 4.5 handles the routine
  "should I bet on this?" question in ~1s; Sonnet 4.6 is the deeper-
  analysis tier for ambiguous edges. Both versions have the methodology
  prompt cached identically so switching tiers doesn't bust the cache.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# Default fast path. Haiku 4.5 has 200K context, ~$1/$5 per 1M tokens —
# right cost profile for an "explain every market" hot path.
MODEL_FAST = "claude-haiku-4-5"

# Opt-in deeper-analysis tier. Sonnet 4.6 catches subtleties Haiku misses
# (multi-factor risk tradeoffs, cross-station correlations). ~3x the cost.
MODEL_DEEP = "claude-sonnet-4-6"

VALID_MODELS = {MODEL_FAST, MODEL_DEEP}


# ─── Output schema ────────────────────────────────────────────────────────────
#
# Structured outputs validate against this schema server-side. Keep it
# additive — any future field belongs in `required` with a sensible
# default value the model can choose. Constraints (min/max/format) are
# documented in the system prompt, not the schema (json_schema validation
# in structured outputs has a restricted grammar — no numerical or string
# constraints).

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {
            "type": "string",
            "enum": ["BUY_YES", "BUY_NO", "PASS", "WAIT_AND_SEE"],
            "description": (
                "BUY_YES / BUY_NO when the model edge is large enough to act on; "
                "PASS when the edge is below noise or the market is too thin; "
                "WAIT_AND_SEE when an intraday observation in the next few hours "
                "will materially sharpen the call."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": (
                "high = model + intraday agree and per-station skill is strong; "
                "medium = clear edge but one weak signal; "
                "low = signals disagree or sample size is thin."
            ),
        },
        "headline": {
            "type": "string",
            "description": (
                "One sentence, ≤140 chars. The thing the user should see first. "
                "Lead with the action, not the analysis."
            ),
        },
        "key_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "2-4 short bullets. The load-bearing reasons for the call. "
                "Each fact should reference a specific number from the input."
            ),
        },
        "key_risks": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "1-3 short bullets. What would invalidate the call. Always "
                "include at least one — there is no risk-free trade."
            ),
        },
        "suggested_limit_cents": {
            "anyOf": [{"type": "integer"}, {"type": "null"}],
            "description": (
                "Suggested limit price in cents (1-99), or null for PASS / "
                "WAIT_AND_SEE. For BUY_YES this is the highest price worth "
                "paying given the model probability; for BUY_NO this is the "
                "highest NO price (i.e. 100 - the lowest acceptable YES price)."
            ),
        },
        "tail_warning": {
            "type": "boolean",
            "description": (
                "True when the Gaussian and empirical ensemble probabilities "
                "disagree by more than 5pp, or when the threshold sits in a "
                "fat-tail region of the forecast distribution. Flags that the "
                "headline confidence may be optimistic."
            ),
        },
        "disclaimer": {
            "type": "string",
            "description": (
                "Always present. Required language: \"Not investment advice. \" "
                "followed by any market-specific caveats (e.g. low resolved-"
                "sample count, resolution station ambiguity)."
            ),
        },
    },
    "required": [
        "recommendation",
        "confidence",
        "headline",
        "key_facts",
        "key_risks",
        "suggested_limit_cents",
        "tail_warning",
        "disclaimer",
    ],
    "additionalProperties": False,
}


# ─── System prompt (cached) ───────────────────────────────────────────────────
#
# Long on purpose. The minimum cacheable prefix is 4096 tokens on Haiku
# 4.5 and 2048 on Sonnet 4.6 — so the dashboard's methodology, decision
# logic, station list, and output-schema description all live here, and
# the first request writes the cache (1.25x) while every subsequent
# request reads it (~0.1x). At ~5K tokens cached, the per-call cost on
# Haiku drops from ~$0.025 to ~$0.0025.

_SYSTEM_PROMPT = """\
# narve.ai — Weather Dashboard Actionable-Insight Engine

You are the insight engine for the weather dashboard. Your job: take the
structured market + forecast data the backend assembled, and return one
short, opinionated, JSON-shaped recommendation telling the user what to
do and why.

You do NOT have tools. You do NOT fetch data. The backend has already
gathered everything relevant. Your job is purely synthesis — translate
the numbers into a decision.

## 1. What the dashboard does

The dashboard scrapes weather-related prediction markets from Polymarket
and Kalshi (e.g. "Will NYC's high temperature be above 75°F on May 7?"),
prices them against an 8-NWP-ensemble forecast (GFS, ECMWF, ICON, GEM,
UKMO, JMA, MET.no, BOM), augments with regional high-resolution models
when applicable (HRRR for CONUS, AROME for France, UKMO 2km for UK,
ICON-D2 for Germany, HARMONIE-DINI for Nordics), and computes a model
probability via either a Gaussian fit or the empirical CDF over raw
ensemble members.

A signal is generated when `edge = model_prob - yes_price` crosses a
configurable threshold (typically 5pp). The dashboard also tracks intraday
METAR observations every 5 minutes, so for markets resolving today the
running daily max sharpens the probability dramatically — sometimes
snapping a 50/50 prior to ~99%.

## 2. Input fields

You receive a JSON object with these top-level fields. Some are optional
(absent for some markets). Don't assume presence — check before reading.

### Market identification
- `market_id` — opaque string
- `source` — "polymarket" | "kalshi"
- `kalshi_ticker` — Kalshi ticker if source is kalshi, else absent
- `question` — the natural-language market question
- `city` — canonical city key from STATION_MAP (see §9)
- `target_date` — YYYY-MM-DD UTC date the market resolves on
- `lead_days` — integer days from now to target_date (0 = today)
- `category` — "temperature" | "hurricane" | "precipitation" | etc.
- `volume_usd` — market's lifetime traded volume, when known

### Threshold being scored
- `threshold` — float, °F
- `is_over` — true for "above X°F" markets, false for "below X°F"
- `temp_lower` + `temp_upper` — instead of threshold, for range markets
- `unit` — "F" or "C" (always normalized to F internally)

### Prices (decimal 0.01–0.99)
- `yes_price` — current YES probability priced by the market
- `no_price` — equals 1 - yes_price
- `mid_price` — bid-ask midpoint when known

### Model probability (the headline number)
- `model_prob` — consensus probability the threshold resolves YES
- `model_prob_gaussian` — Gaussian-fit version
- `model_prob_empirical` — empirical-CDF version (from raw ensemble members)
- `model_method` — "empirical" | "gaussian" | "intraday_conditional"
- `tail_warning` — true if gaussian and empirical disagree by >5pp
- `edge` — `model_prob - yes_price` (positive = bet YES)
- `intraday_conditional` — sharper probability incorporating today's METAR

### Forecast distribution
- `forecast.mean` — consensus mean high temperature (°F)
- `forecast.std` — consensus std (post bias-correction, post lead-inflation)
- `forecast.raw_std` — pre-inflation ensemble spread
- `forecast.raw_mean` — pre-downscaling mean
- `forecast.empirical_sigma_floor` — empirical residual std from history
- `forecast.lead_time_mult` — multiplier applied to std for this lead
- `forecast.n_members` — total ensemble member count
- `forecast.n_highres` — number of high-res deterministic models that fired
- `forecast.percentiles` — {p05, p25, p50, p75, p95} of member distribution
- `forecast.bias_corrected` — true if a per-model bias correction was applied
- `forecast.downscaling` — {applied: bool, delta_f, r2, n}

### Intraday (only for markets resolving today)
- `intraday.running_max` — highest °F observed so far today at station
- `intraday.last_obs_f` — most recent METAR temperature
- `intraday.obs_count` — number of METAR cycles polled today
- `intraday.hours_elapsed` — local hours into the day
- `intraday.trajectory` — list of recent {obs_time, temp_f} (newest last)

### Historical skill (per station)
- `station_skill.n_resolved` — resolved-market sample size for this city
- `station_skill.win_rate` — historical win rate of betting the sign of edge
- `station_skill.brier_score` — historical Brier score on this city's signals

## 3. Decision logic

Pick exactly one of four recommendations:

### BUY_YES
Choose this when ALL of these hold:
- `edge >= 0.05` (model says YES is ≥5pp likelier than the market priced)
- `intraday_conditional` (when present) confirms or strengthens the edge
- Bid-ask doesn't eat the edge — suggested limit is at least 1pp under
  `model_prob` and at least 2pp away from `yes_price`
- No critical risk that flips the sign (e.g. resolution-station ambiguity)

### BUY_NO
Choose this when ALL of these hold:
- `edge <= -0.05` (model says YES is ≥5pp LESS likely than market priced)
- `intraday_conditional` (when present) doesn't contradict (e.g.
  running_max hasn't already crossed the threshold)
- Suggested NO limit is achievable: `100 - yes_price - 2`
- Buying NO at the suggested price still leaves model edge after fees

### PASS
Choose this when:
- `abs(edge) < 0.03` — edge is below typical bid-ask + fee drag
- OR `volume_usd` is so thin (<$1000) that the price is informationless
- OR confidence signals disagree strongly (Gaussian vs empirical,
  intraday vs prior, model vs per-station historical track record)

### WAIT_AND_SEE
Choose this ONLY for markets resolving today (`lead_days == 0`) where:
- `intraday.running_max` is currently inconclusive about the threshold
- The next 2-4 hours of METAR observations will resolve the question
  far more cheaply than betting now
- Example: market asks "≥80°F", running_max is 76°F at noon, peak heating
  forecast suggests it'll be close — wait for 2-3pm observations

## 4. Confidence calibration

- `high` — three signals agree: model, intraday (if available), and
  station_skill (`n_resolved >= 20` and `brier_score < 0.20`).
- `medium` — model is clear but one of (intraday, station_skill, ensemble
  agreement) is missing or borderline.
- `low` — at least two signals weak/disagreeing; `tail_warning` true;
  `station_skill.n_resolved < 5`; or `lead_days >= 7` (long-lead
  uncertainty is real).

NEVER claim high confidence on a market where `station_skill.n_resolved`
is below 5 — there's no track record to lean on.

## 5. Sigma calibration and tail risks

The forecast distribution is bias-corrected and the std is the larger of
(ensemble spread, empirical residual floor) inflated by the fitted
lead-time curve. When the threshold sits within ~0.5σ of the mean, the
probability is sensitive to the sigma choice — flag `tail_warning: true`.

When `forecast.tail_warning` (passed in) is true OR your own check
finds Gaussian/empirical disagreement > 5pp, you MUST set
`tail_warning: true` in your output. Don't override the input signal.

## 6. Intraday sharpening

When `intraday.running_max` is present, it's a hard floor on the final
daily max. Two extreme cases:

- **Already past the threshold.** If `running_max >= threshold` for an
  "above" market, the model's prior is overruled — probability snaps to
  ~99%. If `yes_price < 95`, this is a near-arbitrage; confidence high.

- **Forecast peak < threshold.** If peak heating already happened
  (hours_elapsed > 17 local) and running_max is comfortably below the
  threshold, snap the other way.

For unresolved cases mid-day, the `intraday_conditional` field already
computes the conditional probability. Trust it over the prior — that's
the whole point of intraday tracking.

## 7. Suggested limit price

Choose a limit that leaves edge after a notional 2pp fee:

- BUY_YES: round down to `min(model_prob - 2, yes_price + 1)` in cents.
  Never above 95¢; never below 5¢.
- BUY_NO: the NO price is `100 - yes_price`. Suggest at
  `min((1 - model_prob) * 100 - 2, no_price + 1)`. Never above 95¢.
- PASS / WAIT_AND_SEE: null.

If the achievable limit leaves <1pp edge after fees, downgrade to PASS.

## 8. Disclaimer

Always begin with: "Not investment advice." Add market-specific caveats
in the same string when warranted:
- `station_skill.n_resolved < 5` → "Track record on this city is sparse."
- Resolution-station ambiguity (rare; question is unclear on which
  airport) → "Resolution station unclear from the question text."
- `volume_usd < 1000` → "Market is thinly traded; quoted price may not
  reflect executable size."

## 9. Station list (canonical city keys)

These are the cities the dashboard tracks. Use the canonical key
verbatim in the `headline` and `key_facts` (e.g. "Chicago" not "Chi").
Don't invent stations not on this list.

US: new york, nyc, chicago, dallas, miami, los angeles, la, atlanta,
austin, houston, denver, san francisco, seattle.
North America: toronto, mexico city, panama city.
Europe: london, paris, munich, milan, madrid, warsaw, moscow, istanbul,
amsterdam, helsinki.
Middle East / Asia: tel aviv, ankara, tokyo, seoul, busan, hong kong,
shanghai, beijing, shenzhen, chongqing, wuhan, chengdu, taipei,
singapore, kuala lumpur, jakarta, lucknow.
South America: buenos aires, sao paulo.
Oceania: sydney, wellington.

Each is mapped to a specific airport (KLGA for NYC, KORD for Chicago,
KSEA for Seattle, EGLC for London, etc.). The user already knows this;
don't recite the airport codes unless directly relevant.

## 10. Output

Return a single JSON object matching the provided schema. NEVER include
prose outside the JSON, NEVER explain your reasoning meta-commentary
("Here is the analysis…"). The schema validates server-side — if it
fails, the user sees an error.

Fields are required exactly as specified. `key_facts` must reference
specific numbers from the input — "model says 78% vs market 62%" beats
"the model favors YES". `key_risks` must include at least one item,
even on high-confidence calls — there is no risk-free trade.

Keep the headline ≤140 characters and lead with the action verb. Bad:
"The model probability of 78% suggests YES is undervalued at 62 cents."
Good: "Buy YES at 64¢: model says 78%, intraday peak forecast 81°F."

## Examples

### Example 1 — strong BUY_YES with intraday confirmation

Input fragment:
{
  "question": "Will Chicago be above 75°F on 2026-05-07?",
  "city": "chicago",
  "target_date": "2026-05-07",
  "lead_days": 0,
  "yes_price": 0.62,
  "model_prob": 0.84,
  "edge": 0.22,
  "model_method": "intraday_conditional",
  "forecast": {"mean": 78.2, "std": 2.4, "n_highres": 1},
  "intraday": {"running_max": 76.5, "obs_count": 18, "hours_elapsed": 14},
  "station_skill": {"n_resolved": 47, "win_rate": 0.61, "brier_score": 0.18}
}

Output:
{
  "recommendation": "BUY_YES",
  "confidence": "high",
  "headline": "Buy YES at 66¢: Chicago running_max already 76.5°F, model says 84%.",
  "key_facts": [
    "Running max at KORD is 76.5°F at 2pm local — already past the 75°F threshold",
    "Model probability 84% (intraday-conditioned) vs market 62%; edge +22pp",
    "47 resolved Chicago markets in track record at 61% win rate, Brier 0.18"
  ],
  "key_risks": [
    "Resolution station is KORD (O'Hare); if question references a different station the threshold may not have been crossed"
  ],
  "suggested_limit_cents": 66,
  "tail_warning": false,
  "disclaimer": "Not investment advice."
}

### Example 2 — PASS due to thin signal

Input fragment:
{
  "question": "Will Madrid be above 90°F on 2026-08-15?",
  "lead_days": 90,
  "yes_price": 0.55,
  "model_prob": 0.57,
  "edge": 0.02,
  "forecast": {"mean": 90.4, "std": 5.8, "lead_time_mult": 2.3},
  "intraday": null,
  "station_skill": {"n_resolved": 3}
}

Output:
{
  "recommendation": "PASS",
  "confidence": "low",
  "headline": "Pass: 2pp edge at 90-day lead is below noise.",
  "key_facts": [
    "Edge is +2pp (model 57% vs market 55%); below the 5pp signal threshold",
    "Lead-time multiplier 2.3x at 90 days — forecast std 5.8°F brackets the threshold",
    "Only 3 resolved Madrid markets — track record is statistically insignificant"
  ],
  "key_risks": [
    "Long-lead forecasts are dominated by climatology drift; the +2pp could flip on any model run"
  ],
  "suggested_limit_cents": null,
  "tail_warning": false,
  "disclaimer": "Not investment advice. Track record on this city is sparse."
}

### Example 3 — BUY_NO with tail warning

Input fragment:
{
  "question": "Will Miami be below 70°F on 2026-05-08?",
  "city": "miami",
  "lead_days": 1,
  "yes_price": 0.18,
  "model_prob": 0.04,
  "model_prob_gaussian": 0.04,
  "model_prob_empirical": 0.11,
  "edge": -0.14,
  "tail_warning": true,
  "forecast": {"mean": 76.2, "std": 3.1, "n_highres": 1,
               "percentiles": {"p05": 71.2, "p50": 76.5, "p95": 81.0}},
  "station_skill": {"n_resolved": 28, "win_rate": 0.57, "brier_score": 0.21}
}

Output:
{
  "recommendation": "BUY_NO",
  "confidence": "medium",
  "headline": "Buy NO at 80¢: market overpricing a cold-Miami tail at 18% vs model 4-11%.",
  "key_facts": [
    "Forecast mean 76.2°F with p05 71.2°F — even the 5th-percentile member doesn't cross the 70°F threshold",
    "Gaussian model says 4%, empirical CDF over members says 11% — tail-warning flag set",
    "Market priced at 18% YES; betting NO at 80¢ leaves >7pp edge after fees"
  ],
  "key_risks": [
    "Empirical/Gaussian disagreement of 7pp means the fat tail is real — a single GFS run swinging cold could move the market",
    "Miami's coastal sea-breeze can drop morning lows below daytime forecast minima; threshold is the high, but a flash front is non-zero"
  ],
  "suggested_limit_cents": 80,
  "tail_warning": true,
  "disclaimer": "Not investment advice."
}

### Example 4 — WAIT_AND_SEE near peak heating

Input fragment:
{
  "question": "Will Dallas be above 95°F on 2026-07-15?",
  "city": "dallas",
  "lead_days": 0,
  "yes_price": 0.55,
  "model_prob": 0.62,
  "edge": 0.07,
  "intraday": {"running_max": 91.5, "last_obs_f": 93.0,
                "obs_count": 14, "hours_elapsed": 12},
  "forecast": {"mean": 95.4, "std": 1.8, "n_highres": 1},
  "station_skill": {"n_resolved": 31, "win_rate": 0.62}
}

Output:
{
  "recommendation": "WAIT_AND_SEE",
  "confidence": "medium",
  "headline": "Wait: Dallas at 93°F at noon with peak heating 2-3 hours away — 2pm METAR resolves this cheaply.",
  "key_facts": [
    "Running max 91.5°F at noon local; peak heating window is 14:00-17:00 — climb of 3.5°F needed",
    "Forecast mean 95.4°F with std 1.8°F — threshold sits inside one sigma of the mean",
    "Cost of waiting 2-3 hours is one METAR cycle; price move on the next observation is likely larger than current edge"
  ],
  "key_risks": [
    "If a cumulus deck builds and caps the high, the +7pp model edge inverts faster than the market reprices"
  ],
  "suggested_limit_cents": null,
  "tail_warning": false,
  "disclaimer": "Not investment advice."
}

## 11. Resolution mechanics

Two important distinctions:

**Polymarket** resolves to the question's named source. Many weather
markets say "according to the National Weather Service for [airport]"
— that maps to ASOS/AWOS daily summary, which is the same data feed
the dashboard's METAR poller reads. Some Polymarket markets reference
"AccuWeather" or "Weather Underground" instead — these can deviate
from ASOS by 1-2°F on edge cases. When the question text references a
non-NWS source, add a `key_risk` flagging the deviation.

**Kalshi** resolves to the specific ICAO ticker named in the series
(e.g. KXHIGHNY → KLGA in NYC, KXHIGHCHI → KORD in Chicago). Kalshi
weather resolutions are unambiguous and match our METAR data exactly.

When you see a Polymarket and a Kalshi market on the same threshold for
the same city/date and the prices differ by >3pp, that's a cross-venue
mispricing — typically a stale Polymarket book. Flag this as a risk on
the cheaper side (could be re-priced quickly) and an opportunity on the
expensive side.

## 12. Cross-source signal corroboration

The dashboard already passes you the consensus probability — but pay
attention to which models agreed. When `n_highres >= 1` (HRRR, AROME,
UKMO 2km, ICON-D2, HARMONIE-DINI for the relevant region), short-lead
confidence rises substantially. When the high-res block contributed and
sits within 0.5σ of the global ensemble mean, that's strong agreement;
mention it in `key_facts`. When high-res disagrees with global by more
than 1σ, that's a tail risk worth flagging — the high-res model often
sees a synoptic feature global ensembles smooth over.

## 13. Edge cases and common confusions

**Range markets** (`temp_lower` + `temp_upper`): probability is
`P(lower ≤ X ≤ upper)`, not `P(X ≥ lower) - P(X ≥ upper)`. The
backend computes this correctly — don't second-guess the math. The
trap is treating range markets like above/below markets when crafting
the headline. For ranges, lead with the band: "65-70°F range market".

**Markets with absurd thresholds** (asking "above 110°F" in a cold
city like Seattle): if the model probability is below 1% and the market
is priced at 2-3%, the absolute edge is small but the RELATIVE
mispricing is huge. Still PASS — the absolute payoff dollars don't
compensate for the variance.

**Markets with `target_date` in the past**: shouldn't reach you (the
backend filters), but if one does, the answer is always PASS with
`key_risks` flagging the stale market.

**Volume below $1000**: the quoted yes_price may not be executable —
the book may be one-sided with a 10pp bid-ask. Always include "Market
is thinly traded" in `key_risks` when `volume_usd < 1000`, even if the
recommendation is still BUY_*. The user can decide whether to bother.

**Downscaling correction > 5°F** (`forecast.downscaling.delta_f`): the
regression is making an aggressive call. Useful when `r2 > 0.7` and
`n > 50`. Suspicious when `r2 < 0.4` or `n < 30`. Flag as a risk in the
borderline case rather than blindly trusting the corrected mean.

**Lead time 0 with no intraday data**: rare but possible if METAR poll
failed for this station. Treat as `lead_days = 0` for confidence
purposes but mention the missing intraday observation in `key_risks`
— it's a meaningful information disadvantage.

**`station_skill` with `win_rate < 0.45`**: the model has been WORSE
than coin flips on this city historically. Don't override the model —
the small sample on weather markets means historical win rate is high
variance — but downgrade confidence to `low` and mention the
underperformance in `key_risks`.

## 14. Tone and brevity

The audience is a sophisticated retail user reading on a small panel
while watching live prices. Optimize for:

- **First-glance comprehension.** The headline should answer "what do
  I do?" in one sentence with one number.
- **Numbers over adjectives.** "+22pp edge" beats "significant edge".
  "76.5°F running max" beats "well above the threshold".
- **Specificity over hedging.** If the model says BUY_YES, say
  BUY_YES — don't say "the model leans bullish but watch for risks".
  The whole point of this engine is decisive synthesis.

Avoid:
- "It's important to note that…" / "It's worth considering…"
- Repeating the question text verbatim in the headline
- Generic risk language ("market conditions can change") — risks must
  be specific and load-bearing
- Probability point estimates implied to more precision than you have
  ("the probability is 78.34%" — write "78%")

## 15. Final reminders

- Output is JSON only, no surrounding prose.
- Every field in the schema is required.
- `key_risks` has at least one item, always.
- `disclaimer` begins with "Not investment advice."
- `headline` ≤ 140 chars, leads with the action verb.
- `suggested_limit_cents` is null for PASS / WAIT_AND_SEE, otherwise
  an integer in [1, 99].
- Don't claim `high` confidence with `station_skill.n_resolved < 5`.
- When in doubt between PASS and a marginal BUY_*, prefer PASS — false
  positives hurt the user's PnL; false negatives just leave money on
  the table for next time.
"""


def _system_blocks() -> list[dict]:
    """Build the system prompt as a list of text blocks with a cache
    breakpoint on the last block. This is the prefix that gets cached —
    keep it byte-identical between calls."""
    return [
        {
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


# ─── User message construction ────────────────────────────────────────────────

def build_user_message(context: dict) -> str:
    """Serialize the market context as the user turn.

    Sorted keys + compact separators so the bytes are deterministic —
    matters for tests, and incidentally tidies the model's view of the
    data. The string preamble is short on purpose; the methodology
    lives in the system prompt, not here.
    """
    payload = json.dumps(context, sort_keys=True, separators=(",", ":"),
                         default=str)
    return (
        "Analyze this market and produce the JSON recommendation.\n\n"
        f"Market context:\n{payload}"
    )


# ─── Streaming wrapper ────────────────────────────────────────────────────────

@dataclass
class StreamChunk:
    """One event yielded by `stream_insight`. The endpoint translates
    these into SSE frames for the frontend."""
    type: str  # "token" | "complete" | "error"
    data: dict


def _client():
    """Lazy-import + construct so the rest of the module can be imported
    without the anthropic SDK installed (handy for tests that mock the
    wrapper). Raises with a clear hint when ANTHROPIC_API_KEY is unset."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; insight engine cannot run. "
            "Add it to the dashboard's environment before enabling /api/insight."
        )
    import anthropic
    return anthropic.Anthropic()


def stream_insight(context: dict, *,
                   model: str = MODEL_FAST,
                   client=None) -> Iterator[StreamChunk]:
    """Yield StreamChunks for one insight call.

    Parameters
    ----------
    context : dict
        Already-assembled per-market context (see `build_user_message`).
    model : str
        Either `MODEL_FAST` (Haiku, default) or `MODEL_DEEP` (Sonnet).
        Other values are coerced to MODEL_FAST so a stray query param
        can't pick an arbitrary expensive model.
    client : anthropic.Anthropic, optional
        Injectable for tests. Defaults to a fresh client from env.
    """
    if model not in VALID_MODELS:
        logger.info("insight: unknown model %r, falling back to %s",
                    model, MODEL_FAST)
        model = MODEL_FAST

    cli = client if client is not None else _client()

    try:
        with cli.messages.stream(
            model=model,
            max_tokens=2048,
            system=_system_blocks(),
            messages=[{
                "role": "user",
                "content": build_user_message(context),
            }],
            output_config={
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        ) as stream:
            # Forward text deltas verbatim so the frontend can show
            # progress / partial JSON as it streams.
            for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield StreamChunk("token",
                                          {"text": event.delta.text})

            final = stream.get_final_message()
    except Exception as e:
        logger.warning("insight stream failed: %s", e)
        yield StreamChunk("error", {"error": str(e), "type": type(e).__name__})
        return

    # Extract the JSON payload. structured outputs guarantees the
    # response is a single text block of valid JSON matching the schema;
    # we still wrap json.loads in a try since SDK errors can in theory
    # produce an empty content array.
    text = next((b.text for b in final.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
        logger.warning("insight: JSON parse failed on final message text=%r", text[:200])

    usage = {
        "input_tokens": final.usage.input_tokens,
        "output_tokens": final.usage.output_tokens,
        "cache_creation_input_tokens": getattr(final.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(final.usage, "cache_read_input_tokens", 0) or 0,
    }
    yield StreamChunk("complete", {
        "insight": parsed,
        "usage": usage,
        "model": final.model,
        "stop_reason": final.stop_reason,
    })


# ─── Context digestion helpers ────────────────────────────────────────────────

def _percentiles(members: list[float], qs=(0.05, 0.25, 0.50, 0.75, 0.95)) -> dict:
    """Quick percentile dict from raw ensemble members."""
    if not members:
        return {}
    sorted_m = sorted(float(m) for m in members if m is not None)
    n = len(sorted_m)
    if n == 0:
        return {}
    out = {}
    for q in qs:
        idx = q * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        v = sorted_m[lo] * (1 - frac) + sorted_m[hi] * frac
        out[f"p{int(q * 100):02d}"] = round(v, 1)
    return out


def digest_forecast(forecast: Optional[dict]) -> Optional[dict]:
    """Trim a full forecast dict down to what the model needs.

    Drops the per-model breakdown and the full ensemble member list
    (often 200+ floats); keeps percentiles + summary stats. Bounded
    payload size makes per-call cost predictable.
    """
    if not forecast:
        return None
    members = forecast.get("ensemble") or []
    out = {
        "mean": forecast.get("mean"),
        "std": forecast.get("std"),
        "raw_mean": forecast.get("raw_mean"),
        "raw_std": forecast.get("raw_std"),
        "min": forecast.get("min"),
        "max": forecast.get("max"),
        "n_members": len(members),
        "n_highres": forecast.get("n_highres", 0),
        "highres_models": forecast.get("highres_models") or [],
        "lead_time_mult": forecast.get("lead_time_mult"),
        "bias_corrected": forecast.get("bias_corrected", False),
        "n_bias_models": forecast.get("n_bias_models", 0),
        "empirical_sigma_floor": forecast.get("empirical_sigma_floor"),
        "downscaling": forecast.get("downscaling"),
        "source": forecast.get("source"),
        "percentiles": _percentiles(members) if members else {},
    }
    return out


def digest_intraday(running: Optional[dict],
                    trajectory: Optional[list]) -> Optional[dict]:
    """Pack the intraday signal into one tight dict. Trajectory truncated
    to the last 12 observations so we don't blow context budget on
    over-detailed METAR history."""
    if not running and not trajectory:
        return None
    if trajectory and len(trajectory) > 12:
        trajectory = trajectory[-12:]
    return {
        "running_max": (running or {}).get("running_max"),
        "last_obs_f": (running or {}).get("last_obs_f"),
        "obs_count": (running or {}).get("obs_count", 0),
        "trajectory": trajectory or [],
    }


def assemble_context(*, market: dict, forecast: Optional[dict],
                     temp_info: dict,
                     intraday_running: Optional[dict] = None,
                     intraday_trajectory: Optional[list] = None,
                     station_skill: Optional[dict] = None,
                     model_prob_breakdown: Optional[dict] = None) -> dict:
    """Pack everything the LLM needs into one dict.

    The endpoint calls this with values it already pulled from the
    server's existing helpers. Keeping the assembly in this module
    means tests don't need to import server.py.
    """
    yes_price = market.get("yes_price")
    no_price = round(1 - yes_price, 4) if yes_price is not None else None
    model_prob = (model_prob_breakdown or {}).get("probability") if model_prob_breakdown else market.get("model_prob")
    edge = (round(float(model_prob) - float(yes_price), 4)
            if model_prob is not None and yes_price is not None else None)

    return {
        "market_id": market.get("market_id"),
        "source": market.get("source"),
        "kalshi_ticker": market.get("kalshi_ticker"),
        "question": market.get("question"),
        "city": market.get("city"),
        "target_date": market.get("target_date"),
        "lead_days": market.get("lead_days"),
        "category": market.get("category"),
        "volume_usd": market.get("volume"),
        "threshold": temp_info.get("threshold"),
        "is_over": temp_info.get("is_over"),
        "temp_lower": temp_info.get("temp_lower"),
        "temp_upper": temp_info.get("temp_upper"),
        "unit": temp_info.get("unit", "F"),
        "yes_price": yes_price,
        "no_price": no_price,
        "model_prob": model_prob,
        "model_prob_gaussian": (model_prob_breakdown or {}).get("gaussian"),
        "model_prob_empirical": (model_prob_breakdown or {}).get("empirical"),
        "intraday_conditional": (model_prob_breakdown or {}).get("intraday_conditional"),
        "model_method": (model_prob_breakdown or {}).get("method"),
        "tail_warning": (model_prob_breakdown or {}).get("tail_warning", False),
        "edge": edge,
        "forecast": digest_forecast(forecast),
        "intraday": digest_intraday(intraday_running, intraday_trajectory),
        "station_skill": station_skill,
    }

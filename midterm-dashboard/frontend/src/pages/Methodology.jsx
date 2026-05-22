import React from 'react'
import { Link } from 'react-router-dom'

// Static "how the forecast works" page. Lives at /methodology so the API
// landing page can link to it, and so the brand can point journalists here.
// Updates are markdown-style in-component for now; if it grows we can
// move to MDX or a CMS.

function Section({ title, children }) {
  return (
    <section className="mb-10">
      <h2 className="text-xl font-semibold text-stone-900 mb-3">{title}</h2>
      <div className="prose-stone prose-sm max-w-none space-y-3 text-stone-700 text-sm leading-relaxed">
        {children}
      </div>
    </section>
  )
}

function Inline({ children }) {
  return <code className="px-1.5 py-0.5 rounded bg-stone-100 text-[12px] font-mono text-stone-800">{children}</code>
}

export default function Methodology() {
  return (
    <div className="max-w-3xl mx-auto">
      <div className="mb-8">
        <h1 className="text-3xl font-semibold text-stone-900">How the forecast works</h1>
        <p className="text-stone-500 text-sm mt-2">
          narve.ai is a Brier-weighted ensemble of public prediction markets, polling, and
          forecasting platforms, layered with cross-source smart-money detection, news →
          market reaction measurement, and a common-factor swing model for conditional /
          wave-election scenarios. This page documents every signal end-to-end.
        </p>
      </div>

      <Section title="Sources">
        <p>The ensemble currently includes:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Polymarket</strong> — Gamma API (politics events, tag-filtered) + CLOB for token-level price history.</li>
          <li><strong>Kalshi</strong> — official trade API v2, elections + politics categories.</li>
          <li><strong>PredictIt</strong> — public market-data feed (post-CFTC wind-down; may be sparse).</li>
          <li><strong>Polling</strong> — 538 CSV (legacy) plus RealClearPolling HTML scrape via the embedded <Inline>__NEXT_DATA__</Inline> blob.</li>
          <li><strong>Manifold</strong> — public API, scoped to election groups (us-politics, 2026-midterms, …).</li>
          <li><strong>Metaculus</strong> — community-prediction median for binary election questions.</li>
        </ul>
        <p>Each source is fetched every 5 minutes. Race matching is keyed by structured tags where available (Polymarket), falling back to title regex over canonical state/office vocabularies.</p>
      </Section>

      <Section title="The Brier-weighted ensemble">
        <p>For each race we have up to six independent probabilities. The forecast combines them as a weighted mean:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Cold start</strong> (default): each source gets a prior weight derived from public-aggregate research — real-money markets (Polymarket, Kalshi) at 1.0, play-money / forecasting platforms (Manifold, Metaculus) at 0.7, polling at 0.5, PredictIt at 0.3.</li>
          <li><strong>Brier-weighted</strong>: once a source has at least 5 resolved races in the historical backtest, its weight becomes <Inline>1 / mean_brier_score</Inline>, capped to <Inline>[0.05, 50]</Inline> so a single anomalously-small Brier can't dominate.</li>
          <li><strong>Confidence</strong> combines coverage (fraction of available source weight that produced a probability) × agreement (1 − source spread × 2).</li>
        </ul>
        <p>The <Link to="/backtest" className="underline text-stone-900">backtest page</Link> shows current per-source Brier scores and a calibration scatter against the curated historical-results dataset.</p>
      </Section>

      <Section title="Calibration">
        <p>Beyond per-source Brier scores, the backtest page reports the ensemble's <em>reliability</em> by confidence bucket: of the races we called at 80-100%, what fraction actually resolved D? A perfectly-calibrated forecast has the realized rate equal to the mean forecast within every bucket.</p>
        <p>Today's measurement is in-sample (the same resolved races feed both the Brier weights and the calibration check). It becomes forward-looking as 2026 races resolve — at that point the <Inline>in_sample</Inline> flag in <Inline>/v1/calibration</Inline> flips to <Inline>false</Inline>.</p>
      </Section>

      <Section title="Smart-money signal">
        <p>The sibling <em>top-traders-dashboard</em> service scans top-quality Polymarket wallets (ranked by sustainable Bayesian edge) for their currently open positions. For each midterm race we join those flows against the Polymarket markets stored for the race by slug, classify each outcome as D or R using the same Yes/No-aware classifier the divergence engine uses, and aggregate:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Total smart $</strong> positioned across all matched outcomes.</li>
          <li><strong>Distinct wallet count</strong> contributing.</li>
          <li><strong>Direction</strong> (D / R) and <strong>lean strength</strong> (party share of $).</li>
        </ul>
        <p>When the smart-money direction disagrees with the ensemble lean, the race is flagged as a <em>smart-money divergence</em> — surfaced in amber across the dashboard.</p>
      </Section>

      <Section title="News-to-market lag">
        <p>Every 5 minutes we ingest political RSS from AP, Politico, The Hill, Reuters, NPR, and ABC. Each headline is tagged to a race when both an office keyword (senate / house / governor) AND a state-resolving signal (full state name, politician shortcut, or unambiguous postal code) are present. State-only or office-only headlines do not match.</p>
        <p>For every tagged news event we query the price-history snapshots straddling the publish timestamp. The baseline is the snapshot immediately prior; the reaction is the first subsequent snapshot whose top-outcome price moved ≥1pp. The reported <Inline>lag_seconds</Inline> is the time from publish to that snapshot. Aggregating across events yields the per-source median repricing lag.</p>
      </Section>

      <Section title="Synthetic calls (election-night mode)">
        <p>A race is "called" only when the ensemble forecast crosses 0.90 (for D) or 0.10 (for R) <em>and</em> ensemble confidence is at least 0.55 <em>and</em> the smart-money direction agrees (or is unavailable). Any disagreement near the threshold demotes the call to a lean. This is intentionally conservative — mis-calling a race on race night is the worst failure mode.</p>
        <p>These are <strong>narve.ai synthetic calls</strong>, not Associated Press / Decision Desk HQ decision-desk calls.</p>
      </Section>

      <Section title="Conditional & wave scenarios">
        <p>We model cross-race correlation as a single common-factor "national swing" variable. Conditioning on one race's outcome translates to a swing update in logit space, which propagates to every other race scaled by:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><strong>Competitive sensitivity</strong> — <Inline>4·p·(1−p)</Inline>. A coin-flip race moves most; a 95% race barely budges.</li>
          <li><strong>Pairwise correlation</strong> — region factor × chamber factor. Same-region same-chamber pairs correlate at ≈0.85; cross-region cross-chamber at ≈0.36.</li>
          <li><strong>Hard cap</strong> at ±20pp so highly-correlated pairs can't slam to 0/1 implausibly.</li>
        </ul>
        <p>The wave-election slider applies the same model with a fixed user-chosen swing instead of one inferred from a conditioned race. The Monte-Carlo chamber summary draws 1500 swing values from N(0,1) to give smoother expected-seat counts than naive <Inline>forecast_d ≥ 0.5</Inline> counting.</p>
      </Section>

      <Section title="Public API">
        <p>Read-only JSON endpoints under <Inline>/v1/*</Inline> mirror the most useful data. CORS is permissive; no API key required. Schema is stable within v1 (additive changes only).</p>
        <p>Index: <Inline>GET /v1</Inline>. Highlights:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><Inline>/v1/forecasts</Inline> — all races</li>
          <li><Inline>/v1/forecast/{'{race_key}'}</Inline> — one race</li>
          <li><Inline>/v1/forecast/conditional?given=senate_PA=D</Inline></li>
          <li><Inline>/v1/forecast/wave?swing_pp=5</Inline></li>
          <li><Inline>/v1/election-night</Inline></li>
          <li><Inline>/v1/smart-money/{'{race_key}'}</Inline></li>
        </ul>
      </Section>

      <Section title="Embeds">
        <p>Drop any of these into an article as an <Inline>&lt;iframe&gt;</Inline>:</p>
        <ul className="list-disc pl-5 space-y-1">
          <li><Inline>/embed/forecast/{'{race_key}'}</Inline> — single forecast card (add <Inline>?theme=dark</Inline> for dark mode)</li>
          <li><Inline>/embed/chamber/{'{senate|house|governor}'}</Inline> — chamber control strip</li>
          <li><Inline>/embed/map/{'{senate|house|governor}'}</Inline> — tile-grid US map coloured by call state</li>
        </ul>
        <p>Embeds are framing-allowed (<Inline>frame-ancestors *</Inline>); the rest of the site stays <Inline>X-Frame-Options: DENY</Inline>.</p>
      </Section>

      <Section title="Caveats">
        <ul className="list-disc pl-5 space-y-1">
          <li>The historical-results dataset is hand-curated and small. Brier weights stabilise as 2026 races resolve.</li>
          <li>Polling sources are degraded post-538-shutdown; RCP scrape is best-effort and depends on their HTML markup.</li>
          <li>Conditional-forecast correlations are model-based, not estimated from historical wave data. They produce sensible relative ordering but the absolute magnitudes are heuristic.</li>
          <li>Synthetic calls are not decision-desk calls. Do not bet on them on race night without verifying against AP / DDHQ.</li>
        </ul>
      </Section>

      <div className="mt-8 pt-6 border-t border-stone-200 text-xs text-stone-500">
        Data is public and free to reuse with attribution to <a href="https://midterm.narve.ai" className="underline">narve.ai</a>.
      </div>
    </div>
  )
}

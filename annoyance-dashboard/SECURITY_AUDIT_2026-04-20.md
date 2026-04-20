# Security audit — 2026-04-20

Scope: spike-detection pipeline + confidence scoring (annoyance-dashboard,
tracks P1 / P4 / P8). This doc records residual risks the team chose to
ship with, the reason each one was accepted, and the mitigations that kept
it below the "block launch" bar.

Reviewer: P4 owner, post-P8 merge.
Commit context: `ee05546` + this document's accompanying diff.

---

## P4.1 — Coordinated multi-source gaming (MEDIUM, accepted)

### Threat

An attacker runs two Reddit accounts plus two Bluesky accounts (total
investment: four free accounts + CAPTCHA solving, maybe $5 of proxies).
They post about a target entity during the same hour: two Reddit posts,
two Bluesky posts. The spike detector's multi-source gate
(`spike_detector._apply_multi_source_gate`) fires because:

- ≥2 sources contributed (reddit + bluesky) ✔
- Each source contributed ≥2 posts ✔
- `count >= 5` total posts — close call, may need ≥3 posts on one side ✔

The resulting spike hits the dashboard as a real signal. If it fires the
email path (`notifications.send_spike_email`), it reaches every Pro
subscriber with a CTA link to the entity page. Subscribers who trust the
signal route to prediction markets and bet on a bogus story.

### Why this passes the bar to ship anyway

1. **Cost of attack vs. value.** The attacker's marginal gain is the
   subscribers they route into mispriced markets. narve.ai markets are
   thin; an attacker attempting to front-run this mispricing from the
   other side must also absorb the same market's adverse-selection risk
   the instant it's published. The attack is self-cancelling at small
   scale and the ROI is bad.
2. **Defence-in-depth is already in place.** Sample posts on every spike
   card are sensitive-blurred by default (decision #14). Users see the
   entity name + confidence score before they see content, so a
   zero-content-reveal user still gets the "70 confidence" signal. Low
   confidence reads as red, warning against acting.
3. **FP flag path closes the loop.** Any Pro subscriber who sees a
   questionable spike clicks ⚑ → writes a reason → it lands in
   `/admin/fp-queue` for human review. The new sources/authors ratio
   surfaced there (see "Mitigation" below) makes gaming instantly
   visible to the reviewer.
4. **Email rate limit.** Per-user 5/day cap means even a successful
   gaming attack can't spam a user to death; the Pre-Release rollout
   (README → "Launch checklist — email notifications") keeps the first
   live deploy to an allowlist of one address.
5. **Future work is already designed.** P5 will populate
   `backtest_hit_rate` per entity, shifting the confidence component
   from the current flat 0.5. Entities that history says fire false
   should decay toward 0 confidence naturally. See P4.2 below for why
   the current warmup default is safe even before P5 lands.

### Mitigation landed in this PR

`db.get_entity_hourly_source_stats(entity, hour_iso)` now returns
per-source `unique_authors` alongside `posts`. The multi-source gate
doesn't change its pass/fail logic — we still gate on post count, not
author count, because raising the bar would block legitimate small
stories — but the ratio is written to `spikes.sources_json` on every
insert and rendered on the admin FP queue UI as a pill strip:

    REDDIT 4 posts · 2 authors   BLUESKY 2 posts · 1 author

Any source where `posts / unique_authors >= 3` gets an amber "suspicious"
pill with a tooltip. Reviewers looking at a flagged spike can spot
coordinated gaming in the sub-second it takes to read the row. This
doesn't prevent the attack — it just makes a successful attack extremely
cheap to identify, which raises attacker cost because they can no longer
shelter behind volume-without-breadth.

### Residual risk

A well-capitalized attacker using ≥3 real-looking accounts per source is
still not caught by the pill (`posts/authors ratio = 1.0`, looks legit).
Defence against this class requires actual account-age / prior-history
analysis, which is out of scope for MVP. Logged as "future work" in
COORDINATION.md.

### Sign-off condition

Ship-as-is. Revisit if the admin FP queue shows ≥5 suspicious-pill fires
per week for two weeks running — that's the "the ratio signal alone
isn't enough" threshold.

---

## P4.2 — Warmup confidence floor is hardcoded at 30 (LOW, intentional)

### Finding

`spike_detector._compute_confidence(warmup=True)` returns `30.0`
unconditionally, bypassing the z / multiple / backtest components.

### Why this is correct, not a bug

During warmup (fewer than `config.MIN_BASELINE_HOURS` hours of history
for an entity, or fewer than 3 same-hour-of-week observations), there is
no baseline. The components would be computed against `z=0.0` and
`multiple=0.0` and would return a confidence of 12.5 (all from the
neutral backtest default), which is

- too low — the spike fired on absolute thresholds
  (`count>=WARMUP_MIN_COUNT AND avg_annoyance>=WARMUP_MIN_AVG_ANNOYANCE`),
  which IS real signal; and
- misleading — the tier would render red (<40 in the UI
  colorscheme), implying "we disbelieve this" when the actual message
  is "we cannot yet judge it against the entity's history".

A flat 30 says the true thing: low-medium confidence, warning label on,
but not dismissing the signal. The UI's amber band (40-69) would be too
hopeful; the red (<40) too dismissive. 30 threads the needle — user
sees "take with salt" coloring (we round down into the red band) but
isn't told the number is garbage.

### Why a future reviewer should not "tune" this

- Raising it above ~35 would put warmup fires into the amber/green band
  and over-claim confidence we don't have.
- Lowering it below ~25 would hide valid early signal (every warmup
  fire would look indistinguishable from a rejected one).
- The docstring in `spike_detector._compute_confidence` now carries
  this rationale inline so it's caught at review time, not rediscovered
  from a bug report.

### Sign-off condition

Ship-as-is. Revisit after P5 populates per-entity
`backtest_hit_rate` — at that point warmup entities with *ever-existent*
history elsewhere could inherit a prior, and the flat 30 could be
replaced by `bt * 30` or similar. Not a reason to block anything now.

---

## What this doc does NOT cover

- P1 (auth enforcement) — owned by a different reviewer.
- P5 (backtest framework) — not yet shipped; confidence treats
  hit-rate as neutral (0.5) until it is.
- Supply-chain / dependency scanning — tracked in the gateway repo's
  CI, not here.
- SMTP provider spoofing — mitigated by gateway-level DKIM/SPF which
  this module doesn't touch. Annoyance-dashboard only supplies the
  templated HTML.
- User-level rate limiting beyond 5/day/email — addressed in the
  launch checklist (README), not in this audit.

---

Last updated: 2026-04-20. If you are making a change that could
invalidate any of the reasoning above, update or supersede this doc in
the same commit.

# Pitch Deck — Fact-Check List & Notes

Companion to `narve-pitch-deck.pptx` (14 slides, pre-seed, pre-launch framing).
Every slide carries full speaker notes inside the .pptx (View → Notes).

## Placeholders you must fill before sending

- Slide 1: founder name + contact
- Slide 14: founder bio, raise amount, instrument, use of funds, runway

## Verify before this deck leaves the building

External stats were researched via web search on 2026-07-02, but primary pages
were unreachable from the build sandbox (proxy 403s), so figures come from
search-result snippets cross-checked across 2+ outlets. Spot-check each item:

1. NETWORK CAVEAT (blanket): every external market stat in this deck was sourced from search-result snippets because primary pages returned 403 via the sandbox proxy. Before sending, spot-check the ~10 headline numbers against primary URLs: Pew ~$24B/month (Apr 2026), Bernstein $240B/2026 + $1T/2030, ICE ~$2B into Polymarket, Kalshi $1B @ $22B (Coatue), CFTC NPRM dates (Jun 2026, comments due Jul 27), $3.7B-vs-$91M (Forbes/Seedtable), a16z ~99.7% LLM cost decline, Robinhood 12B+ event contracts 2025, Research and Markets $6.5B→$13.25B TAM, Burton-Taylor $49.2B.

2. 'LLM catches roughly 3-5x more signal than regex alone' comes from the repo's own .env.example — an internal, unbenchmarked claim. Either run a real benchmark or keep it labeled 'internal benchmark' as the deck does.

3. Polymarket '$1B annualized revenue ~6 weeks after US relaunch' traces to Crypto Briefing/Quartz only — thinly sourced; verify before speaking it (it lives in speaker notes, not on a slide).

4. Kalshi figures ($178B annualized volume, $1.5-2B annualized revenue, 2M MAT, 90% US share, +800% institutional) are all company-reported from Kalshi's own newsroom; a third-party estimate implied only ~80-85K Kalshi uniques in Apr 2026. Deck keeps them off slides; if asked, cite only Polymarket's 678K on-chain figure.

5. Verso specifics (YC backing, ~15K contracts, LLM news engine) are sourced from the polymark.et directory and one Crypto Briefing article — verify against Verso's own site/X before printing any specifics; rival terminals 'Paradigm' and 'Panther' are unverified and were omitted.

6. Dome acquisition by Polymarket and subsequent shutdown (used as 'seed-stage exit proof' in slide 11 notes) — verify the acquisition terms and timeline before citing to investors.

7. Test count corrected against the repo: 211 test functions across 16 test files (grep-verified in Dashboard-x-truth-research-prediction/app/tests/); the research bundle's '18 files' figure was wrong. Confirm the count again at send time if the suite changes.

8. Bundle branding and pricing conflict (verified in repo): gateway/server.py BUNDLE_PLANS says 'betyc Trader' $49/mo and 'betyc Pro' $149/mo, while STRIPE_SETUP.md proposes 'narve.ai Trader' $99/$999 and 'Pro' $229/$1,999. Deck uses the coded $49/$149 — normalize the brand name and commit to one lineup before any investor demo.

9. Truth Research is flagged hidden AND parked in gateway/config.json (verified) — it is not publicly listed. Never say 'live'; the deck consistently says 'launch-ready' / 'built and gateway-wired'.

10. Retail research-tool pricing comps (Finviz Elite ~$299.50/yr, Stock Rover ~$249/yr) come from vendor-adjacent articles — verify current list prices on the vendors' own sites before printing.

11. SAM (~$0.9-2.5B/yr by 2029-30) and SOM (~$5-25M ARR) are company-derived estimates, not third-party figures — the slides label them 'company estimate'; keep that label in every export.

12. Monthly-volume methodology conflict: Pew/Forbes cite ~$24B combined notional for Apr 2026 while bitcoin.com cites $8.6B one-sided 'taker volume' for the same month. Deck uses Pew consistently — footnote the methodology if challenged.

13. Regulatory framing: the Third Circuit ruling (Apr 2026) is a preliminary-injunction decision, not a merits ruling; NV/MD/OH/TN federal courts reached contrary conclusions and multiple states are actively fighting prediction markets. The speaker notes hedge this — do not strip the hedge.

14. PunditTracker prior-art dates (2012-~2015, now defunct) used verbally in slides 2/11 notes — confirm the history before citing in the room.

15. Polymarket/Kalshi 'reported talks' valuations ($15-20B Polymarket, ~$40B Kalshi) are unconfirmed press reports — kept out of the deck entirely; if raised in Q&A, say 'reportedly in talks', never 'raised'.

16. X API cost claim ($100/mo Basic tier, 10K tweets/mo cap) comes from the repo's .env.example/README — confirm current X API pricing tiers before using it in the use-of-funds math.

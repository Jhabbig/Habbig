# Annoyance Dashboard -- Locked Decisions

All 17 decisions plus sub-decisions are final. Do not re-litigate.

## Core Decisions

1. **Pricing:** $14.99/mo, $149/yr
2. **Classifier:** Haiku binary pass/skip -> Sonnet full classify on passes
3. **Retention:** classifications forever, raw content dropped at 30d
4. **Access:** hard paywall
5. **Auth:** crypto-dashboard SSO pattern (server.py:81-127)
6. **Notifications:** email only
7. **Positioning:** one product, two views (happiness/annoyance toggle). Live as of 2026-05-14 — ternary polarity (positive/negative/neutral); see sub-decision E.
8. **All entity types equal**
9. **Spike target:** 5-10/day
10. **Confidence UI:** blended z + backtest, 0-100 bar
11. **FP feedback:** flag button -> review queue, no auto-tune
12. **Summary model:** Haiku
13. **Sources:** Reddit + Bluesky, abstract SourceBase ABC (HN/X/others deferred)
14. **Moderation:** content warning + blur, default blurred
15. **Real-time:** 60s polling
16. **Deploy:** scp + fuser + staging subdomain
17. **Market routing:** scaffold entity_markets.json from ALIASES (~100 entities) with `https://narve.ai/markets/search?q={entity}` placeholders

## Sub-decisions

### A1: Classifier Pipeline Detail
Haiku outputs binary pass/skip only (skip if annoyance < 20 AND no named entity). Sonnet does full classification (score + sentiment + entities + primary_topic + is_sensitive) on passes only. One authoritative score per post.

### B: Spike Excerpt Caching
spikes.sample_excerpts_json cached at insertion (first 200 chars x 3 sample posts) so spike cards remain readable after 30d raw TTL.

### C: Market Routing Scaffold
entity_markets.json scaffolded from config.ALIASES with placeholder search-URL; real curation post-merge.

### E: Happiness View Unlock (2026-05-14)
- The classifier already emits ``sentiment`` ∈ {angry, frustrated, neutral, positive}. Ternary polarity derived by mapping {angry, frustrated} → negative, neutral → neutral, positive → positive. **No classifier rewrite.** No incremental Claude spend; the data is already present in ``classifications.sentiment``.
- Schema: a single ``polarity`` column on the ``spikes`` table, default ``'negative'``. Backward-compat — every pre-existing spike row stays in the annoyance view. Migration: ``annoyance-dashboard/migrations/_001_add_polarity.py`` (idempotent ALTER TABLE + index). Same change mirrored in ``db._COLUMN_MIGRATIONS``.
- Detector: ``happiness.py`` is a positive-polarity sibling of ``spike_detector.py``. Same z + multiple + warmup gates, walks ``classifications.sentiment = 'positive'`` rows.
- Endpoints: ``GET /api/happiness/spikes`` (filters ``polarity='positive'``) and ``GET /api/happiness/entities`` (entities with ≥5 positive mentions in last 30d). Same paywall + rate-limit guard.
- UI: ``static/index.html`` ships the second view section ``#happiness-view``; tab toggle is SPA-style (URL hash ``#annoyance`` / ``#happiness``). Stays monochrome — polarity signaled by border weight (2px on ``.spike-card.positive``), never colour.

## Additional Clarifications

- **Sensitive content detection:** is_sensitive + sensitive_reason in Sonnet output (single classifier pass, not separate).
- **Spike threshold defaults:** z >= 3, mult >= 3, count >= 5 ship as-is; calibrate to 5-10/day after 48h live data.
- **Existing code state:** Schema already migrated. config.py already has model IDs + pricing + cost ceiling. Don't redo that work.

## Secrets Hygiene

**Rule:** In this file, and in every document, prompt, commit message, log line,
or PR description under this project, reference environment variables by
**NAME only, never VALUE**.

- Allowed: `ANTHROPIC_API_KEY`, `GATEWAY_SSO_SECRET`, `SENTRY_DSN_ANNOYANCE`,
  `SMTP_PASS`, `DAILY_COST_CEILING_CENTS`, etc.
- Not allowed in this file (or any shared doc): the literal secret value,
  production hostnames, API keys, tokens, passwords, DSNs, connection strings,
  or any other credential material.
- Secrets live only in `.env` files (gitignored) and server-side
  `~/.gateway_env` / `~/.gateway_env_staging`. Nowhere else.
- If a document needs to show an example value, use an obvious placeholder
  (`sk-ant-xxxxxxxx`, `https://o000000.ingest.sentry.io/0000000`) and label
  it as a placeholder.

Applies retroactively: if a secret was pasted anywhere in this project's
docs, rotate the secret immediately and scrub the document.

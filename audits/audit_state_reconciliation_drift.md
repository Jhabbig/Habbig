# Audit — STATE_RECONCILIATION.md drift check

**Date:** 2026-05-15
**Source doc:** `/Users/shocakarel/Habbig/STATE_RECONCILIATION.md` (header dated 2026-04-29T10:21Z)
**Local tip checked:** `283fcbd` (branch `feature/platform-build`, 2 commits ahead of origin)
**Method:** Read each "current reality snapshot" claim from the doc, run the equivalent check against current code, and flag every mismatch. No code changes.

---

## Summary

- **Drift count:** 11 stale claims (8 LOC/file claims, 1 migration-chain claim, 1 templates-coverage claim, 1 env-var claim) plus 1 fix-since-then.
- **Doc age vs current state:** 16 days. Most LOC counts and the migration chain have moved meaningfully; one of the doc's own "look broken" items (the `120_collections.py` orphan) has been fixed in the interim.

## Top 3 stale claims

1. **`server.py` is 8639 lines, doc says 7123** — drifted +1516 lines (+21%) in 16 days. Largest single LOC delta.
2. **`gateway/admin_routes.py` is 3103 lines, doc says 1733** — drifted +1370 lines (+79%). Doc's "Phase 1 key files presence check" prints a stale length.
3. **107 migration files on disk, last revision is `189_sessions_hash_at_rest.py`; doc says 93 files, last is `174_system_secrets.py`** — 14 new migrations (175–189) landed since the doc was written. Anything in the doc that quotes the chain HEAD or the migration count is wrong.

---

## Full drift list

### Source-file sizes (Phase 1)

| File | Doc claim (lines) | Current (lines) | Delta | Stale? |
|---|---:|---:|---:|---|
| `gateway/server.py` | 7123 | 8639 | +1516 | YES |
| `gateway/db.py` | 1394 | 1533 | +139 | YES |
| `gateway/admin_routes.py` | 1733 | 3103 | +1370 | YES |
| `gateway/features.py` | 140 | 169 | +29 | YES |
| `gateway/impersonation.py` | 218 | 218 | 0 | no |
| `gateway/email_system/service.py` | 270 | 289 | +19 | YES |
| `gateway/security/audit.py` | 298 | 390 | +92 | YES |
| `gateway/middleware/subproduct.py` | 141 | 141 | 0 | no |
| `gateway/realtime/hub.py` | 257 | 257 | 0 | no |
| `gateway/scheduler/scheduler.py` | 302 | 302 | 0 | no |
| `gateway/ai/client.py` | 422 | 422 | 0 | no |
| `gateway/og_routes.py` | 182 | 182 | 0 | no |
| `gateway/i18n/translator.py` | 111 | 111 | 0 | no |
| `gateway/forensics/extract_watermark.py` | 291 | 291 | 0 | no |

Confirmed present: `gateway/cache/` is still a package (`__init__.py`, `service.py`, `ttl.py`, plus a new `CACHE.md`). `gateway/cache.py` is still missing as a single module — the doc is right on that point.

### HTML templates (Phase 1)

| Metric | Doc claim | Current | Stale? |
|---|---:|---:|---|
| Total `.html` in `static/` | 102 | 138 | YES |
| Missing `og:image` | 91 | 126 | YES |

The `og:image` coverage problem got worse in absolute terms (+35 templates without OG metadata), and the doc's "12-ish public-facing templates that need triage" recommendation needs to be re-scoped against the larger pool.

### Migration chain (Phase 2)

| Metric | Doc claim | Current | Stale? |
|---|---:|---:|---|
| Migration files on disk | 93 | 107 | YES |
| Last migration filename | `174_system_secrets.py` | `189_sessions_hash_at_rest.py` | YES |
| Revision range | 001 → 174 | 001 → 189 | YES |
| `030_data_exports.py` revision="032" anomaly | present | still present | no |
| `120_collections.py` `down_revision` | "119" (orphan) | **"117" (fixed)** | YES — doc's high-severity finding has been resolved |

The doc's "HIGH — `120_collections.py` orphan reference" is no longer an open issue. It was the first item in the doc's "Top 5 things that look broken" list; it should be struck.

### Env vars (Phase 6)

| Metric | Doc claim | Current | Stale? |
|---|---:|---:|---|
| Distinct env vars referenced in code | 41 | 105 | YES |
| Distinct env vars in `gateway/.env.example` | 85 | 126 | YES |

Both numbers grew substantially. The doc's specific "44 documented vars are stale" list cannot be relied on — the inventory needs to be regenerated against the current `gateway/.env.example`.

### Status docs at repo root (Phase 7)

Doc claims four files are missing: `LEAK_PROTECTION_STATUS.md`, `ERROR_HANDLING.md`, `DB_HEALTH.md`, `BROWSER_COMPAT.md`. All four are still missing — claim is **current**, not stale.

Several new repo-root status docs landed since the doc (not enumerated by the doc but visible at `/Users/shocakarel/Habbig/`): `API_STABILITY.md`, `DEPLOY.md`, `ENV_DEFAULTS_AUDIT.md`, `HARDCODED_URLS_AUDIT.md`, `I18N_HANDOFF.md`, `LANDING_2026_05_14_FINAL.md`, `LARGE_DATA_BENCHMARKS.md`, `PER_FILE_AUDITS.md`, `RACE_CONDITIONS.md`, `SCREEN_SHARE_TESTING.md`, `SECURITY_LOG.md`, `SERVER_STASH_INVENTORY.md`, `STASH_INVENTORY.md`, `STRIPE_GO_LIVE.md`, `UX_STATES_BEFORE_AFTER.md`, and `README.md`. The doc's Phase 7 inventory has not kept pace.

### Server-vs-local drift (Phase 4)

Doc says "SSH unreachable, Phase 4 incomplete." Not re-attempted in this audit. Local tip has moved: doc says `437844d`, current is `283fcbd`. Phase 4 remains unverified.

### Items confirmed accurate

- `gateway/cache.py` is still a package, not a file (doc correct).
- `030_data_exports.py` still has `revision = "032"` (doc correct).
- The four missing status docs at repo root are still missing (doc correct).
- All "0 line delta" rows in the table above (8 of 14 key-file checks).

---

## Recommended re-runs before next batch

1. Regenerate Phase 1 file sizes (every "~N lines" claim).
2. Regenerate Phase 2 (107 files now, HEAD is 189) and update the "Top 5 broken" list — `120_collections.py` is no longer a problem.
3. Regenerate Phase 6 env-var counts and the documented-but-unused list (the 44-entry list in the doc is stale).
4. Regenerate Phase 7 status-doc inventory to include the 16+ new repo-root markdown files.
5. Phase 4 still needs a Tailscale-connected run to verify server tip.

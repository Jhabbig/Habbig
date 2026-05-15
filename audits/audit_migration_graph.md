# Migration graph audit — 2026-05-15

**Verdict: BROKEN — chain is a DAG with 10 heads, not a single linear list.**

Scope: every `gateway/migrations/*.py` (110 migration modules, plus
`__init__.py`). Read-only audit; no migration files were modified.

> Note: the repo was being actively edited during this audit. The
> counts and head list reflect the working-tree state at the time of
> the final re-scan immediately before this file was committed.

## How the runner uses revision/down_revision

`gateway/migrations/__init__.py::upgrade_to_head()` does **not** walk the
`down_revision` linked list. It does:

```python
mods.sort(key=lambda m: m.revision)   # lexicographic sort
to_apply = [m for m in all_mods if m.revision not in applied]
```

It applies every module whose `revision` string is missing from the
`schema_version` table, in lexicographic order. `down_revision` is
declared in every file but **never inspected by the runner**. The
field exists purely for human review / Alembic parity.

That is the saving grace of the anomalies below: a divergent DAG is
still apply-able because the runner ignores the parent pointers. The
liability is that the `down_revision` graph is a misleading lie about
the actual apply order — drift between the declared parent and the
true dependency is silent.

## Chain integrity verdict — BROKEN

| Check                | Result                                    |
| -------------------- | ----------------------------------------- |
| Orphan revisions     | **None.** Every `down_revision` resolves. |
| Cycles               | **None.**                                 |
| Roots                | **Exactly one (`001`).** OK.              |
| Heads                | **10 distinct heads.** EXPECTED 1.        |
| Forks                | **8 nodes with 2+ children.**             |
| Duplicate revisions  | None.                                     |
| Missing `revision=`  | None.                                     |
| File-prefix mismatch | 1 (`030_data_exports.py` → revision 032). |
| Numeric file gaps    | 12 (intentional reserved bands).          |

A correct linear Alembic-style chain has exactly **1 root and 1 head**.
This chain has **1 root and 10 heads**, with **8 internal forks**.

## Anomalies in detail

### 1. Ten heads (revisions that nothing points back at)

A "head" is a `revision` value that no other migration declares as its
`down_revision`. With ten heads, there is no single "latest" tip and
`current_revision()` in `__init__.py` (which returns `sorted(applied)[-1]`
— another lexicographic guess) cannot reliably name the head.

| Head | File                              | Why it's a dead-end                                                |
| ---- | --------------------------------- | ------------------------------------------------------------------ |
| 027  | `027_prediction_extractions.py`   | Sibling to 023; never tied back into the trunk.                    |
| 028  | `028_market_categorisations.py`   | Sibling to 022; never tied back in.                                |
| 029  | `029_source_summaries.py`         | Branched from 024; never tied back in. Also superseded by 052.     |
| 031  | `031_user_predictions.py`         | Branched from 025; never tied back in.                             |
| 075  | `075_user_privacy_prefs.py`       | Branched from 073; trunk continued through 074→080 (not 075).      |
| 097  | `097_perf_baseline_snapshots.py`  | Branched from 095; trunk continued through 096→100 (not 097).      |
| 124  | `124_take_resolution.py`                   | Branched from 120→121→122→123; trunk continued through 120→126.    |
| 125  | `125_preferred_language.py`                | Branched from 116; trunk continued through 116→117 (not 125).      |
| 192  | `192_impersonation_token_hash.py`          | Sibling branch off 188 via 191; trunk continued through 188→193.   |
| 193  | `193_subscriptions_expires_at_backfill.py` | The "real" head — the only head that lies on the main trunk.       |

### 2. Eight internal forks

These are revisions named as the `down_revision` of 2+ later migrations.
Once a fork exists the chain is no longer a list:

| Parent | Children                     | Notes                                                              |
| ------ | ---------------------------- | ------------------------------------------------------------------ |
| 019    | 020, 021                     | First fork. Both children survive into the trunk.                  |
| 020    | 022, 023                     | 022 → 027 (orphan head). 023 → 028 (orphan head).                  |
| 021    | 024, 025, 026                | Triple-branch. 024 → 029 (head). 025 → 031 (head). 026 → 032/030. |
| 073    | 074, 080                     | 074 → 075 (head). 080 continues the trunk.                         |
| 095    | 096, 100                     | 096 → 097 (head). 100 continues the trunk.                         |
| 116    | 117, 125                     | 125 is a head. 117 continues.                                      |
| 120    | 121, 126                     | 121 → 122 → 123 → 124 (head). 126 continues.                       |
| 188    | 191, 193                     | 191 → 192 (head). 193 continues as the trunk head.                 |

The reusable pattern is: a one-off branch off the trunk that the author
never re-anchored. Those branches are the 9 dead heads above (plus the
1 live trunk head, 193).

### 3. Filename / revision mismatch — `030_data_exports.py`

`030_data_exports.py` declares:

```python
revision = "032"
down_revision = "026"
```

The next file `033_affiliate_program.py` declares `down_revision = "032"`,
so the chain *does* connect, but the file is misnamed: filename prefix
`030`, revision `"032"`. Two impacts:

1. The runner sorts modules by `revision`, so apply order is fine.
2. A grep for "migration 030" returns this file; a grep for revision 030
   returns nothing (revision 030 does not exist).

There is no revision `030` and no revision `031`'s parent named `030` —
revision `031` exists (`031_user_predictions.py`, `down_revision = "025"`)
but it is on the orphaned 025 branch, not on the trunk. The trunk goes
`026 → 032 → 033`, skipping the names `030` and `031` as revision values.

Rename suggestion: `030_data_exports.py` → `032_data_exports.py`. Same
contents. Cosmetic only; runner is unaffected because it sorts on the
declared `revision` string.

### 4. Numeric gaps in filenames (informational, intentional)

These are reserved bands, not missing migrations. The runner is keyed on
`revision`, not on filename, so gaps in numbering have no effect.

| Gap range | Skipped numbers |
| --------- | --------------- |
| 031 → 033 | 032 (consumed by `030_data_exports.py` as revision="032") |
| 035 → 050 | 036–049 (reserved band; AI / cost analytics) |
| 064 → 070 | 065–069 (reserved band; security) |
| 075 → 080 | 076–079 (reserved band; indexing) |
| 081 → 090 | 082–089 (reserved band; onboarding) |
| 097 → 100 | 098–099 (reserved band) |
| 100 → 105 | 101–104 (reserved band; jobs) |
| 105 → 110 | 106–109 (reserved band; sharing) |
| 117 → 120 | 118–119 (reserved band; collections) |
| 130 → 161 | 131–160 (largest gap; "drill_runs" jump after the 2026-04-23 freeze line) |
| 162 → 170 | 163–169 (reserved band) |
| 188 → 191 | 189–190 (reserved band; sessions_hash + impersonation_token_hash branch) |
| 188 → 193 | 189–192 (reserved band before subscriptions_expires_at_backfill on the trunk) |

The `161_drill_runs.py` source line confirms gap-style reservations are
intentional:

```python
down_revision = "130"  # Last known landed migration as of 2026-04-23
```

So the 130 → 161 jump is a deliberate reset of the numbering after a
landed freeze. Not a chain break.

### 5. Cycle check — clean

A walk from every revision following `down_revision` terminates at `001`
(whose `down_revision = None`). No cycles.

## Topology, picture

```
001 → 002 → 003 → 004 → 005 → 006 → 007 → 008 → 009 → 010 → 011 → 012
 → 013 → 014 → 015 → 016 → 017 → 018 → 019 ─┬─→ 020 ─┬─→ 022 → 027 ✗
                                            │        └─→ 023 → 028 ✗
                                            └─→ 021 ─┬─→ 024 → 029 ✗
                                                     ├─→ 025 → 031 ✗
                                                     └─→ 026 → 032(030_data_exports) → 033 → … → 073
073 ─┬─→ 074 → 075 ✗
     └─→ 080 → 081 → 090 → 091 → 092 → 093 → 094 → 095 ─┬─→ 096 → 097 ✗
                                                         └─→ 100 → 105 → 110 → 111 → 112 → 113 → 114
   → 115 → 116 ─┬─→ 117 → 120 ─┬─→ 121 → 122 → 123 → 124 ✗
                │               └─→ 126 → 127 → 128 → 129 → 130 → 161 → 162 → 170 → 171 → 172
                │                   → 173 → 174 → 175 → 176 → 177 → 178 → 179 → 180 → 181 → 182
                │                   → 183 → 184 → 185 → 186 → 187 → 188 ─┬─→ 191 → 192 ✗
                │                                                      └─→ 193  ← trunk head
                └─→ 125 ✗
```

Legend: `✗` marks a head (no child points back). The only head that is
"real" (i.e. lies on the trunk) is **193**.

## Risk assessment

| Risk                                                                                | Severity | Why                                                                                     |
| ----------------------------------------------------------------------------------- | -------- | --------------------------------------------------------------------------------------- |
| New migration author picks the wrong `down_revision` (one of the 10 heads, not 193) | MEDIUM   | The `down_revision` field is advisory only — picking 124 or 125 still applies cleanly. The misleading link survives forever and the DAG grows. |
| `current_revision()` returns a misleading value                                     | LOW      | It does `sorted(applied)[-1]` — lexicographic, so 193 wins over 097/125/192/etc. anyway. |
| Downgrade order is wrong                                                            | MEDIUM   | `downgrade()` iterates `reversed(sorted(applied))` and ignores `down_revision`. If two siblings (e.g. 074 and 080) have an order dependency, lexicographic downgrade may run them in the wrong order. No evidence today that any sibling pair has such a dependency, but the runner can't enforce one. |
| Future migrations reach for revision strings that already exist on dead branches    | LOW      | Author picks 029 unaware that revision 029 already exists → duplicate revision in `schema_version` → INSERT collision on apply. Mitigation: pre-commit grep. |
| Chain doesn't migrate to Alembic cleanly                                            | HIGH     | If/when SQLAlchemy/Alembic is adopted, Alembic *does* walk `down_revision` and *will* refuse a chain with 10 heads. A future migration is forced. |

## Recommendations (no code changes performed — audit only)

1. **Re-anchor the 9 dead branches** by changing `down_revision` on each
   head's child to thread it back into the trunk. The 9 dead heads are
   027, 028, 029, 031, 075, 097, 124, 125, 192. (For example: set
   `down_revision = "193"` on a tiny no-op `194_merge_branches.py` that
   declares all 9 as predecessors — Alembic supports a tuple
   `down_revision = ("193", "027", "028", "029", "031", "075", "097", "124", "125", "192")`
   for merge migrations. The current raw-sqlite runner accepts a string;
   would need to accept a tuple.) Defer until/unless Alembic is adopted.

2. **Fix the cosmetic filename mismatch on `030_data_exports.py`**. Rename
   to `032_data_exports.py` so filename prefix == revision string. Runner
   unaffected (it sorts on `revision`).

3. **Add a CI check** that imports every module under `gateway/migrations/`,
   builds the parent map, and asserts exactly one root and exactly one head.
   Belongs in `gateway/tests/test_migrations_chain.py`. Would have caught
   the first sibling (020 → 022 + 023) the day it landed.

4. **Document the runner contract** in `gateway/migrations/__init__.py`
   docstring: state explicitly that `down_revision` is advisory and apply
   order is lexicographic `revision` order, so authors don't assume
   Alembic semantics.

## Files audited

110 migration modules under `gateway/migrations/` plus `__init__.py`.
Full revision → down_revision map below (sorted by revision).

```
001 → None        (001_initial_schema.py)
002 → 001         (002_email_unsubscribes.py)
003 → 002         (003_password_reset_hardening.py)
004 → 003         (004_waitlist_positions.py)
005 → 004         (005_account_deletion.py)
006 → 005         (006_security_features.py)
007 → 006         (007_user_sessions_hardening.py)
008 → 007         (008_environmental_impact.py)
009 → 008         (009_predictions_extracted_at_index.py)
010 → 009         (010_credibility_pipeline.py)
011 → 010         (011_retrospectives.py)
012 → 011         (012_calibration.py)
013 → 012         (013_morning_briefing.py)
014 → 013         (014_api_keys.py)
015 → 014         (015_backtests.py)
016 → 015         (016_whale_positions.py)
017 → 016         (017_user_bankroll.py)
018 → 017         (018_telegram_links.py)
019 → 018         (019_remove_2fa.py)
020 → 019         (020_portfolio_integration.py)             [FORK from 019]
021 → 019         (021_status_page.py)                       [FORK from 019]
022 → 020         (022_embed_widgets.py)                     [FORK from 020]
023 → 020         (023_referrals_leaderboard.py)             [FORK from 020]
024 → 021         (024_admin_features.py)                    [FORK from 021]
025 → 021         (025_claude_usage_log.py)                  [FORK from 021]
026 → 021         (026_notifications.py)                     [FORK from 021]
027 → 022         (027_prediction_extractions.py)            [DEAD HEAD]
028 → 023         (028_market_categorisations.py)            [DEAD HEAD]
029 → 024         (029_source_summaries.py)                  [DEAD HEAD]
031 → 025         (031_user_predictions.py)                  [DEAD HEAD]
032 → 026         (030_data_exports.py)                      [FILENAME MISMATCH: 030 → 032]
033 → 032         (033_affiliate_program.py)
034 → 033         (034_push_subscriptions.py)
035 → 034         (035_performance_indexes.py)
050 → 035         (050_ai_cache.py)
051 → 050         (051_claude_usage_log_ext.py)
052 → 051         (052_source_summaries_ext.py)
053 → 052         (053_calibration_and_timing.py)
054 → 053         (054_source_network.py)
055 → 054         (055_backtests.py)
056 → 055         (056_market_movement.py)
057 → 056         (057_weekly_reports.py)
058 → 057         (058_environmental_impact_ext.py)
059 → 058         (059_insider_signals.py)
060 → 059         (060_subproduct_subscriptions.py)
061 → 060         (061_processed_stripe_events.py)
062 → 061         (062_portfolio_integration.py)
063 → 062         (063_telegram_connections.py)
064 → 063         (064_discord_integration.py)
070 → 064         (070_watermark_seeds.py)
071 → 070         (071_forensic_sentinels.py)
072 → 071         (072_security_events.py)
073 → 072         (073_bulk_fetch_counters.py)
074 → 073         (074_claude_cost_controls.py)              [FORK from 073]
075 → 074         (075_user_privacy_prefs.py)                [DEAD HEAD]
080 → 073         (080_query_indexes.py)                     [FORK from 073]
081 → 080         (081_slow_query_log.py)
090 → 081         (090_onboarding_state.py)
091 → 090         (091_first_week_goals.py)
092 → 091         (092_engagement_events.py)
093 → 092         (093_churn_signals.py)
094 → 093         (094_cancellation_flow.py)
095 → 094         (095_schema_drift_backfill.py)
096 → 095         (096_slow_request_log.py)                  [FORK from 095]
097 → 096         (097_perf_baseline_snapshots.py)           [DEAD HEAD]
100 → 095         (100_realtime_connection_events.py)        [FORK from 095]
105 → 100         (105_scheduler_job_runs.py)
110 → 105         (110_shared_market_cards.py)
111 → 110         (111_shared_source_cards.py)
112 → 111         (112_shared_predictions.py)
113 → 112         (113_user_invite_tokens.py)
114 → 113         (114_share_metrics.py)
115 → 114         (115_unified_search_fts.py)
116 → 115         (116_unified_search_populate.py)
117 → 116         (117_search_analytics.py)                  [FORK from 116]
120 → 117         (120_collections.py)
121 → 120         (121_collection_follows.py)                [FORK from 120]
122 → 121         (122_market_takes.py)
123 → 122         (123_take_reports.py)
124 → 123         (124_take_resolution.py)                   [DEAD HEAD]
125 → 116         (125_preferred_language.py)                [DEAD HEAD]
126 → 120         (126_saved_views.py)                       [FORK from 120]
127 → 126         (127_external_forecasts.py)
128 → 127         (128_api_keys_ext.py)
129 → 128         (129_webhooks.py)
130 → 129         (130_feedback.py)
161 → 130         (161_drill_runs.py)                        ["last known landed as of 2026-04-23"]
162 → 161         (162_integrity_cleanup.py)
170 → 162         (170_changelog_seen.py)
171 → 170         (171_onboarding_tour_state.py)
172 → 171         (172_public_profile_fields.py)
173 → 172         (173_user_follows.py)
174 → 173         (174_system_secrets.py)
175 → 174         (175_email_watermarks.py)
176 → 175         (176_trading_addon_settings.py)
177 → 176         (177_newsletter_segments.py)
178 → 177         (178_status_launch_2026_05_14.py)
179 → 178         (179_webhook_hardening.py)
180 → 179         (180_api_keys_origins.py)
181 → 180         (181_wallet_connect_nonces.py)
182 → 181         (182_webhook_dlq_index.py)
183 → 182         (183_newsletter_campaigns.py)
184 → 183         (184_explain_audit_indexes.py)
185 → 184         (185_users_stripe_customer_id.py)
186 → 185         (186_subproduct_feature_flags.py)
187 → 186         (187_newsletter_blast_jobs.py)
188 → 187         (188_fix_users_invite_token_fk.py)                  [FORK from 188 via 191]
191 → 188         (191_sessions_hash.py)                              [FORK from 188]
192 → 191         (192_impersonation_token_hash.py)                   [DEAD HEAD]
193 → 188         (193_subscriptions_expires_at_backfill.py)          [TRUNK HEAD]
```

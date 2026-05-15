# GDPR Export Completeness Audit

**Date:** 2026-05-15
**Database audited:** `gateway/auth.db` (82 tables, 37 with user-PII columns)
**Exporter audited:** `gateway/exports/generator.py::_collect()`

Compares every table in `auth.db` whose schema contains a user-identifying
column (`*_user_id`, `email`, `username`, `ip_address`, `ip`) against the
list of tables `generator.py` actually queries inside `_collect()`.

Auth.db is the only populated SQLite file on the gateway side (`db.db`
exists but is empty), so any user-PII table absent from this report does
not exist on the production schema as of today.

---

## Summary

| Bucket | Count |
| --- | --- |
| User-PII tables in `auth.db` | 37 |
| Tables exported AND present in `auth.db` | 25 |
| Tables present in `auth.db` but **NOT** exported | 13 |
| Tables exported but not present in `auth.db` (forward-compat / other deploy) | 29 |

---

## Tables exported (and present in auth.db)

These are user-data tables that `_collect()` queries and that exist in the
current `auth.db` schema. They count as covered.

| Table | Notes |
| --- | --- |
| `affiliate_accounts` | covered |
| `api_keys` | covered (key value redacted) |
| `audit_log` | covered (admin PII scrubbed) |
| `backtests` | covered |
| `data_export_requests` | covered |
| `email_unsubscribes` | covered |
| `feedback_submissions` | covered |
| `followed_sources` | covered |
| `gifted_subscriptions` | covered |
| `intelligence_conversations` | covered |
| `intelligence_messages` | covered (joined to conversations) |
| `push_subscriptions` | covered |
| `saved_predictions` | covered |
| `subscriptions` | covered |
| `telegram_user_links` | covered |
| `user_bet_history` | covered |
| `user_follows` | covered (both follower + followed sides) |
| `user_market_alerts` | covered |
| `user_market_credentials` | covered (tokens redacted) |
| `user_market_views` | covered |
| `user_positions` | covered |
| `user_prediction_stats` | covered |
| `user_predictions` | covered |
| `user_sessions` | covered (token hash scrubbed) |
| `user_topics` | covered |
| `users` | covered (password material scrubbed) |

## Tables MISSED — present in auth.db with user PII, NOT exported

These rows are silently dropped from every GDPR export. Each one is a
potential Art. 15 (right of access) gap.

| # | Table | PII columns | Why it matters |
| --- | --- | --- | --- |
| 1 | `login_failures` | `ip` | Failed-login attempts associated with the user's IPs. Subject-access request should include this — it is processing of the user's IP and credentials. |
| 2 | `password_resets` | `user_id`, `used_from_ip` | Password-reset audit trail with originating IP. Clearly user data. |
| 3 | `two_fa_attempts` | `user_id`, `ip_address` | 2FA attempt log per user with IP. Direct subject data. |
| 4 | `email_otps` | `user_id`, `ip_address` | One-time email codes issued to the user, with IP. Subject data. |
| 5 | `sessions` | `user_id` | Legacy/secondary session table (separate from `user_sessions`). If still written to it must be exported. |
| 6 | `analytics_events` | `user_id` | Per-user analytics events. Behavioural profiling data — clearly within Art. 15. |
| 7 | `claude_usage_log` | `user_id` | LLM-call usage per user (tokens, cost, prompts metadata). Subject data. |
| 8 | `backtest_runs` | `user_id` | Backtest jobs the user ran (separate from `backtests`). |
| 9 | `backtest_comparisons` | `user_id` | Backtest comparison artifacts. |
| 10 | `affiliate_conversions` | `referred_user_id` | If the *exporting user* was referred by an affiliate, that conversion row references them. |
| 11 | `invite_tokens` | `claimed_by_user_id`, `claimed_by_email`, `target_email` | Invite tokens claimed by or addressed to the user. The exporter pulls `user_invite_tokens` (a different table that does not exist in this DB) but never queries this one. |
| 12 | `newsletter_subscribers` | `email` | If the user's email appears here they are a data subject of the newsletter system. Currently exported only via `email_unsubscribes`, which is the wrong direction. |
| 13 | `enquiries` | `email` | Contact-form / enquiry submissions keyed by email. Pre-account submissions with the user's email should be returned on subject access. |

## Tables exported but NOT present in auth.db

Exporter code queries these — `_safe_query` swallows the `no such table`
error so the export still succeeds. They are either (a) future / planned
schema, (b) created by a migration not yet applied to this local copy of
the DB, or (c) hosted in a separate SQLite file mounted on prod. None of
them currently contribute rows on this machine:

```
cancellation_attempts, changelog_seen, collection_follows, collections,
discord_user_connections, email_send_log, embed_widgets,
engagement_events, feedback_comments, feedback_items, feedback_votes,
kalshi_connections, market_takes, notification_preferences,
notifications, polymarket_connections, referrals, saved_views,
subscription_pauses, take_votes, telegram_connections,
user_first_week_goals, user_invite_tokens, user_onboarding,
user_trading_addon_settings, webhook_subscriptions, weekly_reports,
whale_watchlist
```

Note `whale_watchlist` is documented as living in a separate sqlite DB on
prod, so its absence here is expected.

---

## Top 5 misses (highest priority)

These are the biggest gaps to close, ranked by how clearly they are
"subject data" under GDPR Art. 15 right of access:

1. **`analytics_events`** — every per-user analytics event the gateway
   captures. Pure behavioural profile of the data subject. Currently
   completely absent from the export.
2. **`two_fa_attempts`** — per-user 2FA attempts with `ip_address`. Both
   authentication-history and IP-history; clearly the user's data.
3. **`password_resets`** — reset history with originating IP. Subject
   data and security-relevant.
4. **`login_failures`** — failed-login attempts keyed by IP. Should be
   surfaced for any user whose IP appears (matched against
   `user_sessions.ip_address`).
5. **`claude_usage_log`** — per-user LLM usage / cost log. Direct
   processing record tied to the user, used for billing decisions.

Honourable mention: `sessions` (legacy table parallel to `user_sessions`)
— if it is still being written to by any code path, omitting it is a
silent data-loss in the export. Worth verifying whether it is dead before
deciding whether to add it.

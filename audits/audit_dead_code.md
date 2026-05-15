# Dead Code Audit (pyflakes)

Static analysis of `gateway/` using `pyflakes 3.4.0` to surface unused imports, unused local variables, undefined names, redefinitions, and other lint signals. No code changes are made in this audit — remediation is a follow-up.

- **Tool:** `python3 -m pyflakes gateway/`
- **Scope:** all Python under `gateway/` (228 files with findings)
- **Date:** 2026-05-15
- **Run from:** `/Users/shocakarel/Habbig`

## Summary

| Metric | Count |
|---|---:|
| **Total findings** | **806** |
| Files with findings | 228 |
| Unused imports | 747 |
| Unused local variables | 41 |
| Undefined names | 5 |
| Redefinitions of unused names | 2 |
| F-strings missing placeholders | 11 |

### Important caveat: `gateway/db.py` re-exports

`gateway/db.py` accounts for **286 of 806** findings (35%). Every one is a `from queries.* import (...) # noqa: F401,E402` block — these are intentional re-exports for backward compatibility so historical `db.<name>` call sites keep working after the `queries/` split. Pyflakes does not honor `# noqa` comments, so it flags them anyway. **These are not actual dead code** and should be excluded from any cleanup pass.

Excluding `gateway/db.py`, the corrected total is **520 findings** across 227 files.

Similarly, several `__init__.py` files use re-exports as a public API surface (`gateway/jobs/__init__.py`, `gateway/auth/__init__.py`, `gateway/credibility/__init__.py`). These should be reviewed before removing — if they're imported elsewhere via `from gateway.jobs import X`, the imports are load-bearing.

## Findings by file

Sorted by finding count (descending). Categories: `imp` = unused imports, `var` = unused local variables, `und` = undefined names, `red` = redefinitions, `fs` = f-string missing placeholders.

| File | Total | imp | var | und | red | fs |
|---|---:|---:|---:|---:|---:|---:|
| `gateway/db.py` [re-export] | 286 | 286 | 0 | 0 | 0 | 0 |
| `gateway/server.py` | 28 | 27 | 1 | 0 | 0 | 0 |
| `gateway/jobs/__init__.py` [package init] | 24 | 24 | 0 | 0 | 0 | 0 |
| `gateway/auth/__init__.py` [package init] | 17 | 17 | 0 | 0 | 0 | 0 |
| `gateway/credibility/__init__.py` [package init] | 8 | 8 | 0 | 0 | 0 | 0 |
| `gateway/status_routes.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/scraper/main.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/tests/integration/test_error_handling.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_j_lighthouse.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/queries/data_exports.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/queries/onboarding.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/queries/claude_usage.py` | 6 | 6 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_i18n.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_collections.py` | 5 | 1 | 4 | 0 | 0 | 0 |
| `gateway/tests/test_saved_views.py` | 5 | 3 | 2 | 0 | 0 | 0 |
| `gateway/queries/auth.py` | 5 | 2 | 0 | 3 | 0 | 0 |
| `gateway/queries/environmental.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/queries/intelligence.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/queries/watchlist.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/queries/markets.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/queries/sources.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/queries/topics.py` | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/exports/__init__.py` [package init] | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/ai/__init__.py` [package init] | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/scenarios/__init__.py` [package init] | 5 | 5 | 0 | 0 | 0 | 0 |
| `gateway/security_routes.py` | 4 | 2 | 2 | 0 | 0 | 0 |
| `gateway/intelligence/__init__.py` [package init] | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/insider/__init__.py` [package init] | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_pwa_v2.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_protected_routes.py` | 4 | 3 | 0 | 0 | 1 | 0 |
| `gateway/tests/test_watermark.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_data_export.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_token_first_auth.py` | 4 | 3 | 0 | 0 | 1 | 0 |
| `gateway/tests/browser/test_mobile_quirks.py` | 4 | 0 | 4 | 0 | 0 | 0 |
| `gateway/tests/browser/test_visual_regression.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/queries/subscriptions.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/queries/admin.py` | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/scheduler/__init__.py` [package init] | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/backend/markets/unified_markets.py` | 4 | 3 | 1 | 0 | 0 | 0 |
| `gateway/observability/__init__.py` [package init] | 4 | 4 | 0 | 0 | 0 | 0 |
| `gateway/backtest_routes.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/collections_routes.py` | 3 | 1 | 2 | 0 | 0 | 0 |
| `gateway/billing_routes.py` | 3 | 0 | 0 | 0 | 0 | 3 |
| `gateway/extension_routes.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/webhooks_routes.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/onboarding_routes.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/scraper/scrapers/twitter.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_search.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_credibility_recompute.py` | 3 | 2 | 0 | 1 | 0 | 0 |
| `gateway/tests/test_intelligence_routes.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_stripe_webhook_route.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_markets.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_referrals.py` | 3 | 1 | 0 | 0 | 0 | 2 |
| `gateway/tests/test_auth_flow.py` | 3 | 2 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_sessions_management.py` | 3 | 1 | 2 | 0 | 0 | 0 |
| `gateway/tests/qa/conftest.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/tests/browser/conftest.py` | 3 | 2 | 1 | 0 | 0 | 0 |
| `gateway/jobs/worker.py` | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/external_forecasts/__init__.py` [package init] | 3 | 3 | 0 | 0 | 0 | 0 |
| `gateway/reports_routes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/engagement_routes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/insider_routes.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/admin_routes.py` | 2 | 0 | 2 | 0 | 0 | 0 |
| `gateway/offline_routes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/saved_views_schema.py` | 2 | 0 | 2 | 0 | 0 | 0 |
| `gateway/feedback_routes.py` | 2 | 1 | 0 | 0 | 0 | 1 |
| `gateway/scraper/scheduler.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/scraper/storage/models.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/scraper/transmission/pusher.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/scraper/transmission/receiver.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/scraper/scrapers/metaculus.py` | 2 | 1 | 0 | 0 | 0 | 1 |
| `gateway/email_system/__init__.py` [package init] | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/pipeline/__init__.py` [package init] | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/insider/base.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_rate_limiting.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_edge_scoring.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_changelog.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_session_cookies.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_resolution_polling.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_claude_cost_controls.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_subproduct_access.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_migrations.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_scheduler.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_analytics.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_affiliate.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_notifications.py` | 2 | 0 | 2 | 0 | 0 | 0 |
| `gateway/tests/test_feature_routes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_external_forecasts.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_logout.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_user_predictions.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_weekly_digest.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_admin_audit_log.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_source_profiles.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_cache.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_embed_widgets.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_log_admin.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_changelog_widget.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_market_takes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_i_dark_mode.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_onboarding_flow.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_subproduct_access_flow.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_data_export_flow.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_login_logout_flow.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_leaderboard_flow.py` | 2 | 1 | 0 | 0 | 0 | 1 |
| `gateway/forensics/extract_watermark.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/realtime/routes.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/scripts/a11y_touch_targets.py` | 2 | 1 | 1 | 0 | 0 | 0 |
| `gateway/scenarios/correlation.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/jobs/feedback_digest.py` | 2 | 2 | 0 | 0 | 0 | 0 |
| `gateway/affiliate_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/subproduct_signup_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/sidebar.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/api_keys_routes.py` | 1 | 0 | 0 | 0 | 0 | 1 |
| `gateway/profile_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/og_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/network_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/push_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/ai_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/impersonation.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/stripe_webhook_hardening.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/take_routes.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/pwa_middleware.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/admin_test_emails_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/export_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/routes_referrals.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/saved_views_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/forecast_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/error_handlers.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/backtest.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/user_prediction_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scenarios_routes.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/scraper/tests/test_api.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scraper/tests/test_scrapers.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scraper/scrapers/substack.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/email_system/welcome.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/pipeline/extract_step.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tools/change_queue.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/status_system/uptime.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/insider/congressional_trades.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_ai_modules.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_csrf.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_admin_users.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_feedback_routes.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_email_system.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_credibility_dashboard.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_admin_newsletter.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_versioning.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_churn_and_retention.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_logging.py` | 1 | 0 | 0 | 0 | 0 | 1 |
| `gateway/tests/test_sharing.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_status_page.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_feed.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_public.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_waitlist.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_sentry.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_impersonation.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_portfolio_sync.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_feature_flags.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_db_maintenance.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_onboarding_routes.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_job_queue.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_status_admin.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_billing_portal.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/tests/test_profile.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_email_welcome.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_seo.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_morning_briefing.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_environmental_http.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_admin_pagination.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_docs.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_webhooks.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_password_reset.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_keys_management.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_v1_consensus.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_query_perf.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_api_public_polish.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_foundation_bundle.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_subproducts.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_explain_popover.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_status_monitoring.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_scenarios.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_onboarding_tour.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_settings_billing.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_newsletter_blast_bounding.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_market_resolution.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_edge_cases.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_breadcrumb.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_forensics.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_account_deletion.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_email_watermark.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_migration_188.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/test_email_template_overrides.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_g_mobile.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_b_unauth.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_a_smoke.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_e_style.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_f_ux.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_h_perf.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_c_auth.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/qa/qa_walk_d_admin.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/browser/test_critical_flows.py` | 1 | 0 | 0 | 0 | 0 | 1 |
| `gateway/tests/a11y/test_static_shape.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/conftest.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_offline_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_admin_impersonation_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_prediction_submit_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_cancellation_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_share_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_password_reset_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_subscription_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_signup_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/tests/e2e/test_watchlist_flow.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/queries/audit.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/queries/sharing_metrics.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scheduler/registry.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/backend/markets/movement_detector.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/observability/perf_stats.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scripts/bench_large_data.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/scenarios/scenario.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/portfolio/kelly.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/credibility/network.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/jobs/notification_jobs.py` | 1 | 0 | 0 | 1 | 0 | 0 |
| `gateway/jobs/backend.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/jobs/insider_jobs.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/i18n/format.py` | 1 | 0 | 1 | 0 | 0 | 0 |
| `gateway/external_forecasts/matcher.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/reports/weekly.py` | 1 | 1 | 0 | 0 | 0 | 0 |
| `gateway/reports/__init__.py` [package init] | 1 | 1 | 0 | 0 | 0 | 0 |

## Top 10 files with most findings

1. `gateway/db.py` — **286 findings** — noqa-suppressed re-exports (not real dead code)
2. `gateway/server.py` — **28 findings**
3. `gateway/jobs/__init__.py` — **24 findings** — package init, may be intentional re-exports
4. `gateway/auth/__init__.py` — **17 findings** — package init, may be intentional re-exports
5. `gateway/credibility/__init__.py` — **8 findings** — package init, may be intentional re-exports
6. `gateway/status_routes.py` — **6 findings**
7. `gateway/scraper/main.py` — **6 findings**
8. `gateway/tests/integration/test_error_handling.py` — **6 findings**
9. `gateway/tests/qa/qa_walk_j_lighthouse.py` — **6 findings**
10. `gateway/queries/data_exports.py` — **6 findings**

## Undefined names (5)

These are likely real bugs and worth investigating first — pyflakes thinks the name doesn't exist at all in the file's namespace.

| File | Line | Message |
|---|---:|---|
| `gateway/tests/test_credibility_recompute.py` | 42 | `undefined name 'pid'` |
| `gateway/queries/auth.py` | 576 | `undefined name '_json_2fa'` |
| `gateway/queries/auth.py` | 591 | `undefined name '_json_2fa'` |
| `gateway/queries/auth.py` | 619 | `undefined name '_json_2fa'` |
| `gateway/jobs/notification_jobs.py` | 378 | `undefined name 'enqueue_email'` |

## Redefinitions of unused names (2)

| File | Line | Message |
|---|---:|---|
| `gateway/tests/test_protected_routes.py` | 98 | `redefinition of unused 'server_features' from line 23` |
| `gateway/tests/test_token_first_auth.py` | 242 | `redefinition of unused 'server_features' from line 34` |

## Unused local variables (41)

Mostly `user`/`admin` from auth-dependency calls in routes — these may be intentional (rate-limit / auth side effect) but unused locals here. Test files have many `cid`/`pid`/`nid` patterns where the returned ID was never asserted on.

| File | Line | Variable |
|---|---:|---|
| `gateway/security_routes.py` | 281 | `admin` |
| `gateway/security_routes.py` | 310 | `admin` |
| `gateway/collections_routes.py` | 430 | `user` |
| `gateway/collections_routes.py` | 1098 | `admin` |
| `gateway/server.py` | 3252 | `cfg` |
| `gateway/insider_routes.py` | 186 | `user` |
| `gateway/admin_routes.py` | 1271 | `hourly_alert` |
| `gateway/admin_routes.py` | 1274 | `daily_alert` |
| `gateway/take_routes.py` | 544 | `user` |
| `gateway/saved_views_schema.py` | 387 | `need_source_cred_join` |
| `gateway/saved_views_schema.py` | 399 | `need_predictions_join` |
| `gateway/routes_referrals.py` | 348 | `exc` |
| `gateway/scenarios_routes.py` | 381 | `user` |
| `gateway/tools/change_queue.py` | 537 | `app` |
| `gateway/tests/test_feedback_routes.py` | 737 | `other_id` |
| `gateway/tests/test_churn_and_retention.py` | 179 | `token` |
| `gateway/tests/test_resolution_polling.py` | 89 | `pid` |
| `gateway/tests/test_billing_portal.py` | 178 | `prime` |
| `gateway/tests/test_migrations.py` | 30 | `first` |
| `gateway/tests/test_notifications.py` | 196 | `nid` |
| `gateway/tests/test_notifications.py` | 203 | `n2` |
| `gateway/tests/test_user_predictions.py` | 324 | `pid` |
| `gateway/tests/test_auth_flow.py` | 937 | `uid` |
| `gateway/tests/test_sessions_management.py` | 63 | `raw1` |
| `gateway/tests/test_sessions_management.py` | 64 | `raw2` |
| `gateway/tests/test_weekly_digest.py` | 106 | `result` |
| `gateway/tests/test_collections.py` | 320 | `cid` |
| `gateway/tests/test_collections.py` | 332 | `cid` |
| `gateway/tests/test_collections.py` | 475 | `cid_private` |
| `gateway/tests/test_collections.py` | 495 | `cid` |
| `gateway/tests/test_saved_views.py` | 202 | `v1` |
| `gateway/tests/test_saved_views.py` | 213 | `v1` |
| `gateway/tests/browser/conftest.py` | 67 | `pw` |
| `gateway/tests/browser/test_mobile_quirks.py` | 76 | `pw` |
| `gateway/tests/browser/test_mobile_quirks.py` | 98 | `pw` |
| `gateway/tests/browser/test_mobile_quirks.py` | 112 | `registered` |
| `gateway/tests/browser/test_mobile_quirks.py` | 138 | `pw` |
| `gateway/tests/e2e/test_login_logout_flow.py` | 22 | `email` |
| `gateway/backend/markets/unified_markets.py` | 86 | `active` |
| `gateway/scripts/a11y_touch_targets.py` | 58 | `bad` |
| `gateway/i18n/format.py` | 111 | `min_frac` |

## F-strings missing placeholders (11)

Trivial — an `f"..."` with no `{}` in it. Either drop the `f` prefix or there's a missing variable.

| File | Line | Message |
|---|---:|---|
| `gateway/api_keys_routes.py` | 348 | `f-string is missing placeholders` |
| `gateway/billing_routes.py` | 755 | `f-string is missing placeholders` |
| `gateway/billing_routes.py` | 762 | `f-string is missing placeholders` |
| `gateway/billing_routes.py` | 767 | `f-string is missing placeholders` |
| `gateway/feedback_routes.py` | 773 | `f-string is missing placeholders` |
| `gateway/scraper/scrapers/metaculus.py` | 97 | `f-string is missing placeholders` |
| `gateway/tests/test_logging.py` | 309 | `f-string is missing placeholders` |
| `gateway/tests/test_referrals.py` | 278 | `f-string is missing placeholders` |
| `gateway/tests/test_referrals.py` | 324 | `f-string is missing placeholders` |
| `gateway/tests/browser/test_critical_flows.py` | 53 | `f-string is missing placeholders` |
| `gateway/tests/e2e/test_leaderboard_flow.py` | 66 | `f-string is missing placeholders` |

## Unused imports — full list

There are 747 unused-import findings total. The largest contributors are listed in the per-file table above. Notable observations:

- `gateway/db.py` (286): all are intentional re-exports — IGNORE.
- `gateway/server.py` (27): worth a targeted cleanup pass.
- `gateway/jobs/__init__.py` (24), `gateway/auth/__init__.py` (17), `gateway/credibility/__init__.py` (8): verify whether they're re-exported as the package's public API before removing.
- The long tail (`*_routes.py`, `queries/*.py`, `tests/*`) is generally low (1–6 per file) and amenable to a one-pass sweep.

### Per-file unused-import details (excluding `gateway/db.py`)

Grouped by file; only files with >=2 unused imports shown (to keep the report scannable). Format: `line: 'symbol' imported but unused`.

#### `gateway/server.py` (27 unused imports)

- L343: `orjson`
- L7671: `backend.markets.unified_markets`
- L7672: `backend.markets.portfolio_aggregator.get_combined_portfolio`
- L7672: `backend.markets.portfolio_aggregator.get_combined_orders`
- L7673: `backend.markets.portfolio_signals.enrich_positions`
- L7673: `backend.markets.portfolio_signals.signal_for_position`
- L7674: `backend.markets.encryption.encrypt_token`
- L7674: `backend.markets.encryption.decrypt_token`
- L8056: `datetime as _dt`
- L8145: `server_features`
- L8161: `affiliate_routes`
- L8174: `forecast_routes`
- L8186: `status_routes`
- L8198: `take_routes`
- L8211: `embed_routes`
- L8224: `push_routes`
- L8237: `offline_routes`
- L8250: `admin_jobs_routes`
- L8264: `admin_health_monitor_routes`
- L8279: `admin_cost_alerts_routes`
- L8294: `admin_test_emails_routes`
- L8309: `admin_emails_routes`
- L8324: `admin_integrations_routes`
- L8338: `billing_routes`
- L8353: `stripe_webhook_routes`
- L8366: `engagement_routes`
- L8379: `feedback_routes`

#### `gateway/jobs/__init__.py` (24 unused imports)

- L17: `jobs.registry.job_registry`
- L17: `jobs.registry.register_job`
- L17: `jobs.registry.register_cron`
- L18: `jobs.backend.enqueue_job`
- L18: `jobs.backend.enqueue_cron`
- L18: `jobs.backend.start_worker`
- L18: `jobs.backend.stop_worker`
- L18: `jobs.backend.get_worker_status`
- L18: `jobs.backend.list_recent_jobs`
- L18: `jobs.backend.retry_job`
- L29: `jobs.email_jobs`
- L30: `jobs.embed_jobs`
- L31: `jobs.notification_jobs`
- L32: `jobs.pipeline_jobs`
- L33: `jobs.resolution_jobs`
- L34: `jobs.status_jobs`
- L42: `jobs.forecast_sync`
- L50: `jobs.take_resolution_jobs`
- L60: `jobs.sync_portfolios`
- L66: `jobs.reconcile_subscriptions`
- L72: `jobs.telegram_sends`
- L79: `jobs.invite_replenish`
- L86: `jobs.share_retention`
- L95: `jobs.newsletter_blast_jobs`

#### `gateway/auth/__init__.py` (17 unused imports)

- L25: `auth.cookies.PENDING_TOKEN_COOKIE`
- L25: `auth.cookies.SESSION_COOKIE`
- L25: `auth.cookies.PENDING_TOKEN_TTL`
- L25: `auth.cookies.set_pending_token_cookie`
- L25: `auth.cookies.clear_pending_token_cookie`
- L25: `auth.cookies.read_pending_token`
- L25: `auth.cookies.sign_pending_token`
- L25: `auth.cookies.verify_pending_token`
- L25: `auth.cookies.set_session_cookie_hardened`
- L25: `auth.cookies.clear_session_cookie_hardened`
- L37: `auth.guards.read_hardened_session`
- L37: `auth.guards.attach_session_to_request`
- L37: `auth.guards.require_pending_token`
- L37: `auth.guards.require_hardened_session`
- L37: `auth.guards.require_hardened_admin`
- L37: `auth.guards.require_auth`
- L37: `auth.guards.require_admin`

#### `gateway/credibility/__init__.py` (8 unused imports)

- L14: `credibility.calibration.compute_brier_score`
- L14: `credibility.calibration.reliability_diagram_data`
- L14: `credibility.calibration.brier_component_for_record`
- L19: `credibility.timing.compute_timing_score`
- L22: `credibility.network.classify_relationship`
- L22: `credibility.network.pairwise_stats`
- L22: `credibility.network.echo_chamber_clusters`
- L22: `credibility.network.network_adjusted_consensus`

#### `gateway/status_routes.py` (6 unused imports)

- L25: `asyncio`
- L28: `json`
- L35: `fastapi.Form`
- L36: `fastapi.responses.PlainTextResponse`
- L38: `server`
- L39: `server._role_badge`

#### `gateway/scraper/main.py` (6 unused imports)

- L19: `logging.handlers`
- L23: `datetime.datetime`
- L23: `datetime.timezone`
- L28: `fastapi.responses.JSONResponse`
- L30: `scraper.config.LOG_LEVEL`
- L42: `scraper.transmission.pusher.push_untransmitted`

#### `gateway/tests/integration/test_error_handling.py` (6 unused imports)

- L24: `tests._testdb`
- L352: `common.circuit_breaker.claude_breaker`
- L352: `common.circuit_breaker.stripe_breaker`
- L352: `common.circuit_breaker.polymarket_breaker`
- L352: `common.circuit_breaker.kalshi_breaker`
- L352: `common.circuit_breaker.sec_edgar_breaker`

#### `gateway/tests/qa/qa_walk_j_lighthouse.py` (6 unused imports)

- L17: `os`
- L22: `pytest`
- L24: `.conftest as _conf`
- L41: `.conftest.live_server`
- L46: `sys`
- L46: `contextlib`

#### `gateway/queries/data_exports.py` (6 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`
- L15: `sqlite3`

#### `gateway/queries/onboarding.py` (6 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`
- L15: `sqlite3`

#### `gateway/queries/claude_usage.py` (6 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`
- L17: `typing.Optional`

#### `gateway/tests/test_i18n.py` (5 unused imports)

- L16: `json`
- L17: `os`
- L23: `tests._testdb`
- L26: `i18n.SUPPORTED`
- L249: `server_features`

#### `gateway/queries/environmental.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/queries/intelligence.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/queries/watchlist.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/queries/markets.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/queries/sources.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/queries/topics.py` (5 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`
- L14: `secrets`

#### `gateway/exports/__init__.py` (5 unused imports)

- L9: `exports.generator.EXPORT_DIR`
- L9: `exports.generator.EXPORT_TTL_SECONDS`
- L9: `exports.generator.generate`
- L9: `exports.generator.sign_download_url`
- L9: `exports.generator.verify_download_token`

#### `gateway/ai/__init__.py` (5 unused imports)

- L22: `ai.client.ANTHROPIC_MODELS`
- L22: `ai.client.cost_for`
- L22: `ai.client.get_async_client`
- L22: `ai.client.log_response`
- L22: `ai.client.log_failure`

#### `gateway/scenarios/__init__.py` (5 unused imports)

- L21: `scenarios.correlation.compute_market_correlations`
- L21: `scenarios.correlation.pearson`
- L21: `scenarios.correlation.align_snapshot_series`
- L26: `scenarios.scenario.compute_scenario`
- L26: `scenarios.scenario.estimate_shift`

#### `gateway/intelligence/__init__.py` (4 unused imports)

- L2: `intelligence.context.build_intelligence_context`
- L3: `intelligence.claude_client.INTELLIGENCE_SYSTEM_PROMPT`
- L3: `intelligence.claude_client.stream_intelligence_response`
- L3: `intelligence.claude_client.get_intelligence_response`

#### `gateway/insider/__init__.py` (4 unused imports)

- L24: `insider.base.BaseFetcher`
- L24: `insider.base.FetchResult`
- L24: `insider.base.SignalStrength`
- L24: `insider.base.ALL_FETCHERS`

#### `gateway/tests/test_pwa_v2.py` (4 unused imports)

- L7: `time`
- L16: `tests._testdb`
- L20: `server_features`
- L21: `offline_routes`

#### `gateway/tests/test_watermark.py` (4 unused imports)

- L20: `time`
- L21: `typing.Optional`
- L23: `pytest`
- L27: `tests._testdb`

#### `gateway/tests/test_data_export.py` (4 unused imports)

- L15: `io`
- L22: `pytest`
- L24: `tests._testdb`
- L619: `server_features`

#### `gateway/tests/browser/test_visual_regression.py` (4 unused imports)

- L21: `contextlib`
- L22: `os`
- L23: `pathlib.Path`
- L24: `typing.Any`

#### `gateway/queries/subscriptions.py` (4 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L14: `secrets`

#### `gateway/queries/admin.py` (4 unused imports)

- L10: `hashlib`
- L11: `hmac`
- L12: `json`
- L13: `logging`

#### `gateway/scheduler/__init__.py` (4 unused imports)

- L15: `scheduler.scheduler.scheduler`
- L15: `scheduler.scheduler.record_start`
- L15: `scheduler.scheduler.record_end`
- L16: `scheduler.decorators.scheduled_job`

#### `gateway/observability/__init__.py` (4 unused imports)

- L9: `observability.sentry_setup.init_sentry`
- L9: `observability.sentry_setup.scrub_sensitive_data`
- L9: `observability.sentry_setup.set_user_context`
- L9: `observability.sentry_setup.tag_request`

#### `gateway/backtest_routes.py` (3 unused imports)

- L18: `typing.Any`
- L18: `typing.Optional`
- L21: `fastapi.responses.RedirectResponse`

#### `gateway/extension_routes.py` (3 unused imports)

- L25: `html as _html`
- L29: `secrets`
- L31: `typing.Any`

#### `gateway/webhooks_routes.py` (3 unused imports)

- L22: `time`
- L23: `typing.Optional`
- L26: `fastapi.Form`

#### `gateway/onboarding_routes.py` (3 unused imports)

- L33: `datetime as _dt`
- L34: `hashlib`
- L45: `fastapi.responses.RedirectResponse`

#### `gateway/scraper/scrapers/twitter.py` (3 unused imports)

- L38: `json`
- L41: `re`
- L91: `playwright_stealth.stealth_async`

#### `gateway/tests/test_search.py` (3 unused imports)

- L19: `os`
- L23: `tests._testdb`
- L571: `time as _t`

#### `gateway/tests/test_intelligence_routes.py` (3 unused imports)

- L21: `time`
- L24: `types.SimpleNamespace`
- L25: `typing.Any`

#### `gateway/tests/test_protected_routes.py` (3 unused imports)

- L20: `tests._testdb`
- L21: `db`
- L23: `server_features`

#### `gateway/tests/test_stripe_webhook_route.py` (3 unused imports)

- L31: `tests._testdb`
- L132: `stripe_webhook_routes`
- L264: `stripe_webhook_routes`

#### `gateway/tests/test_markets.py` (3 unused imports)

- L8: `time`
- L10: `unittest.mock.AsyncMock`
- L10: `unittest.mock.MagicMock`

#### `gateway/tests/test_saved_views.py` (3 unused imports)

- L10: `json`
- L19: `tests._testdb`
- L24: `saved_views_routes`

#### `gateway/tests/test_token_first_auth.py` (3 unused imports)

- L31: `tests._testdb`
- L34: `server_features`
- L242: `server_features`

#### `gateway/tests/qa/conftest.py` (3 unused imports)

- L22: `sys`
- L29: `tests._testdb`
- L106: `playwright`

#### `gateway/backend/markets/unified_markets.py` (3 unused imports)

- L8: `dataclasses.field`
- L9: `datetime.datetime`
- L9: `datetime.timezone`

#### `gateway/jobs/worker.py` (3 unused imports)

- L32: `jobs.email_jobs`
- L32: `jobs.notification_jobs`
- L32: `jobs.pipeline_jobs`

#### `gateway/external_forecasts/__init__.py` (3 unused imports)

- L20: `external_forecasts.base.Candidate`
- L20: `external_forecasts.base.ProviderError`
- L20: `external_forecasts.base.PROVIDERS`

#### `gateway/security_routes.py` (2 unused imports)

- L280: `server.render_page`
- L331: `server.render_page`

#### `gateway/reports_routes.py` (2 unused imports)

- L14: `typing.Any`
- L17: `fastapi.responses.JSONResponse`

#### `gateway/engagement_routes.py` (2 unused imports)

- L22: `html`
- L31: `server`

#### `gateway/offline_routes.py` (2 unused imports)

- L18: `os`
- L24: `server`

#### `gateway/scraper/scheduler.py` (2 unused imports)

- L21: `datetime.datetime`
- L21: `datetime.timezone`

#### `gateway/scraper/storage/models.py` (2 unused imports)

- L10: `dataclasses.field`
- L11: `datetime.timezone`

#### `gateway/scraper/transmission/pusher.py` (2 unused imports)

- L24: `datetime.datetime`
- L24: `datetime.timezone`

#### `gateway/scraper/transmission/receiver.py` (2 unused imports)

- L13: `asyncio`
- L17: `typing.Optional`

#### `gateway/email_system/__init__.py` (2 unused imports)

- L14: `email_system.service.EmailService`
- L15: `email_system.unsubscribe.UnsubscribeManager`

#### `gateway/pipeline/__init__.py` (2 unused imports)

- L9: `pipeline.extract_step.process_post`
- L9: `pipeline.extract_step.process_posts_batch`

#### `gateway/insider/base.py` (2 unused imports)

- L20: `asyncio`
- L28: `typing.Any`

#### `gateway/tests/test_rate_limiting.py` (2 unused imports)

- L10: `unittest.mock.patch`
- L14: `security.rate_limiter.limiter as global_limiter`

#### `gateway/tests/test_edge_scoring.py` (2 unused imports)

- L11: `time`
- L16: `tests._testdb`

#### `gateway/tests/test_changelog.py` (2 unused imports)

- L20: `re`
- L25: `tests._testdb`

#### `gateway/tests/test_session_cookies.py` (2 unused imports)

- L30: `tests._testdb`
- L32: `auth.cookies.PENDING_TOKEN_COOKIE`

#### `gateway/tests/test_credibility_recompute.py` (2 unused imports)

- L9: `math`
- L17: `tests._testdb`

#### `gateway/tests/test_claude_cost_controls.py` (2 unused imports)

- L23: `json`
- L27: `time`

#### `gateway/tests/test_subproduct_access.py` (2 unused imports)

- L6: `asyncio`
- L15: `fastapi.Request`

#### `gateway/tests/test_scheduler.py` (2 unused imports)

- L18: `time`
- L21: `tests._testdb`

#### `gateway/tests/test_analytics.py` (2 unused imports)

- L21: `time`
- L164: `tests._testdb`

#### `gateway/tests/test_affiliate.py` (2 unused imports)

- L23: `tests._testdb`
- L27: `server_features`

#### `gateway/tests/test_feature_routes.py` (2 unused imports)

- L13: `tests._testdb`
- L16: `server_features`

#### `gateway/tests/test_external_forecasts.py` (2 unused imports)

- L32: `tests._testdb`
- L36: `server_features`

#### `gateway/tests/test_logout.py` (2 unused imports)

- L20: `tests._testdb`
- L23: `server_features`

#### `gateway/tests/test_auth_flow.py` (2 unused imports)

- L21: `time`
- L56: `server_features`

#### `gateway/tests/test_admin_audit_log.py` (2 unused imports)

- L20: `tests._testdb`
- L25: `queries.audit as audit_queries`

#### `gateway/tests/test_source_profiles.py` (2 unused imports)

- L13: `tests._testdb`
- L16: `server_features`

#### `gateway/tests/test_cache.py` (2 unused imports)

- L22: `tests._testdb`
- L338: `db`

#### `gateway/tests/test_embed_widgets.py` (2 unused imports)

- L36: `tests._testdb`
- L40: `embed_routes`

#### `gateway/tests/test_log_admin.py` (2 unused imports)

- L15: `json`
- L20: `pathlib.Path`

#### `gateway/tests/test_changelog_widget.py` (2 unused imports)

- L14: `json`
- L18: `tests._testdb`

#### `gateway/tests/test_market_takes.py` (2 unused imports)

- L27: `pytest`
- L31: `tests._testdb`

#### `gateway/tests/qa/qa_walk_i_dark_mode.py` (2 unused imports)

- L22: `fastapi.testclient.TestClient`
- L24: `.conftest as _conf`

#### `gateway/tests/browser/conftest.py` (2 unused imports)

- L28: `contextlib`
- L35: `typing.Optional`

#### `gateway/tests/e2e/test_onboarding_flow.py` (2 unused imports)

- L8: `time`
- L11: `tests._testdb`

#### `gateway/tests/e2e/test_subproduct_access_flow.py` (2 unused imports)

- L15: `pytest`
- L16: `tests._testdb`

#### `gateway/tests/e2e/test_data_export_flow.py` (2 unused imports)

- L7: `time`
- L10: `tests._testdb`

#### `gateway/queries/auth.py` (2 unused imports)

- L12: `json`
- L13: `logging`

#### `gateway/forensics/extract_watermark.py` (2 unused imports)

- L42: `typing.Any`
- L276: `sys`

#### `gateway/realtime/routes.py` (2 unused imports)

- L30: `asyncio`
- L36: `typing.Any`

#### `gateway/scenarios/correlation.py` (2 unused imports)

- L32: `typing.Any`
- L32: `typing.Iterable`

#### `gateway/jobs/feedback_digest.py` (2 unused imports)

- L24: `json`
- L27: `time`

### Files with exactly 1 unused import

(121 files)

| File | Line | Symbol |
|---|---:|---|
| `gateway/admin_test_emails_routes.py` | 30 | `asyncio` |
| `gateway/affiliate_routes.py` | 29 | `logging` |
| `gateway/ai_routes.py` | 15 | `typing.Any` |
| `gateway/backend/markets/movement_detector.py` | 33 | `typing.Any` |
| `gateway/backtest.py` | 42 | `typing.Optional` |
| `gateway/collections_routes.py` | 38 | `json` |
| `gateway/credibility/network.py` | 32 | `typing.Any` |
| `gateway/email_system/welcome.py` | 23 | `typing.Optional` |
| `gateway/error_handlers.py` | 28 | `time` |
| `gateway/export_routes.py` | 28 | `typing.Optional` |
| `gateway/external_forecasts/matcher.py` | 33 | `json` |
| `gateway/feedback_routes.py` | 33 | `datetime as _dt` |
| `gateway/forecast_routes.py` | 41 | `server.log as _root_log` |
| `gateway/impersonation.py` | 25 | `typing.Optional` |
| `gateway/insider/congressional_trades.py` | 21 | `insider.base.SignalStrength` |
| `gateway/insider_routes.py` | 19 | `typing.Optional` |
| `gateway/jobs/backend.py` | 295 | `arq` |
| `gateway/jobs/insider_jobs.py` | 79 | `insider` |
| `gateway/network_routes.py` | 18 | `typing.Any` |
| `gateway/observability/perf_stats.py` | 27 | `typing.Optional` |
| `gateway/og_routes.py` | 32 | `fastapi.Request` |
| `gateway/pipeline/extract_step.py` | 18 | `typing.Any` |
| `gateway/portfolio/kelly.py` | 26 | `typing.Optional` |
| `gateway/profile_routes.py` | 28 | `os` |
| `gateway/push_routes.py` | 22 | `typing.Optional` |
| `gateway/pwa_middleware.py` | 19 | `os` |
| `gateway/queries/audit.py` | 25 | `typing.Iterable` |
| `gateway/queries/sharing_metrics.py` | 26 | `typing.Optional` |
| `gateway/reports/__init__.py` | 13 | `reports.weekly.build_report_for_user` |
| `gateway/reports/weekly.py` | 32 | `typing.Any` |
| `gateway/saved_views_routes.py` | 32 | `time` |
| `gateway/scenarios/scenario.py` | 31 | `typing.Any` |
| `gateway/scheduler/registry.py` | 110 | `jobs` |
| `gateway/scraper/scrapers/metaculus.py` | 14 | `typing.Optional` |
| `gateway/scraper/scrapers/substack.py` | 18 | `typing.Optional` |
| `gateway/scraper/tests/test_api.py` | 4 | `unittest.mock.MagicMock` |
| `gateway/scraper/tests/test_scrapers.py` | 3 | `pytest` |
| `gateway/scripts/a11y_touch_targets.py` | 42 | `playwright.sync_api.sync_playwright` |
| `gateway/scripts/bench_large_data.py` | 29 | `pathlib.Path` |
| `gateway/sidebar.py` | 51 | `typing.Optional` |
| `gateway/status_system/uptime.py` | 20 | `status_system.STATUSES` |
| `gateway/stripe_webhook_hardening.py` | 46 | `typing.Any` |
| `gateway/subproduct_signup_routes.py` | 30 | `secrets` |
| `gateway/tests/a11y/test_static_shape.py` | 26 | `os` |
| `gateway/tests/e2e/conftest.py` | 37 | `tests._testdb` |
| `gateway/tests/e2e/test_admin_impersonation_flow.py` | 15 | `tests._testdb` |
| `gateway/tests/e2e/test_cancellation_flow.py` | 14 | `tests._testdb` |
| `gateway/tests/e2e/test_leaderboard_flow.py` | 8 | `tests._testdb` |
| `gateway/tests/e2e/test_login_logout_flow.py` | 7 | `tests._testdb` |
| `gateway/tests/e2e/test_offline_flow.py` | 11 | `tests._testdb` |
| `gateway/tests/e2e/test_password_reset_flow.py` | 11 | `tests._testdb` |
| `gateway/tests/e2e/test_prediction_submit_flow.py` | 15 | `tests._testdb` |
| `gateway/tests/e2e/test_share_flow.py` | 8 | `tests._testdb` |
| `gateway/tests/e2e/test_signup_flow.py` | 12 | `tests._testdb` |
| `gateway/tests/e2e/test_subscription_flow.py` | 13 | `tests._testdb` |
| `gateway/tests/e2e/test_watchlist_flow.py` | 10 | `tests._testdb` |
| `gateway/tests/qa/qa_walk_a_smoke.py` | 21 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_b_unauth.py` | 25 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_c_auth.py` | 19 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_d_admin.py` | 21 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_e_style.py` | 174 | `.conftest.live_server` |
| `gateway/tests/qa/qa_walk_f_ux.py` | 28 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_g_mobile.py` | 27 | `.conftest as _conf` |
| `gateway/tests/qa/qa_walk_h_perf.py` | 26 | `.conftest as _conf` |
| `gateway/tests/test_account_deletion.py` | 9 | `tests._testdb` |
| `gateway/tests/test_admin_newsletter.py` | 30 | `tests._testdb` |
| `gateway/tests/test_admin_pagination.py` | 21 | `tests._testdb` |
| `gateway/tests/test_admin_users.py` | 22 | `tests._testdb` |
| `gateway/tests/test_ai_modules.py` | 26 | `typing.Any` |
| `gateway/tests/test_api_docs.py` | 22 | `tests._testdb` |
| `gateway/tests/test_api_keys_management.py` | 24 | `tests._testdb` |
| `gateway/tests/test_api_public.py` | 16 | `tests._testdb` |
| `gateway/tests/test_api_public_polish.py` | 21 | `tests._testdb` |
| `gateway/tests/test_api_v1_consensus.py` | 28 | `tests._testdb` |
| `gateway/tests/test_api_versioning.py` | 59 | `server_features` |
| `gateway/tests/test_breadcrumb.py` | 26 | `tests._testdb` |
| `gateway/tests/test_collections.py` | 24 | `unittest.mock.AsyncMock` |
| `gateway/tests/test_credibility_dashboard.py` | 9 | `time` |
| `gateway/tests/test_csrf.py` | 15 | `security.csrf.CSRF_TOKEN_LENGTH` |
| `gateway/tests/test_db_maintenance.py` | 27 | `tests._testdb` |
| `gateway/tests/test_edge_cases.py` | 15 | `time` |
| `gateway/tests/test_email_system.py` | 17 | `tests._testdb` |
| `gateway/tests/test_email_template_overrides.py` | 12 | `tests._testdb` |
| `gateway/tests/test_email_watermark.py` | 30 | `tests._testdb` |
| `gateway/tests/test_email_welcome.py` | 18 | `tests._testdb` |
| `gateway/tests/test_environmental_http.py` | 31 | `types.SimpleNamespace` |
| `gateway/tests/test_explain_popover.py` | 23 | `tests._testdb` |
| `gateway/tests/test_feature_flags.py` | 11 | `tests._testdb` |
| `gateway/tests/test_feed.py` | 18 | `tests._testdb` |
| `gateway/tests/test_forensics.py` | 28 | `tests._testdb` |
| `gateway/tests/test_foundation_bundle.py` | 25 | `tests._testdb` |
| `gateway/tests/test_impersonation.py` | 13 | `tests._testdb` |
| `gateway/tests/test_job_queue.py` | 8 | `tests._testdb` |
| `gateway/tests/test_market_resolution.py` | 9 | `tests._testdb` |
| `gateway/tests/test_migration_188.py` | 7 | `sys` |
| `gateway/tests/test_migrations.py` | 7 | `tests._testdb` |
| `gateway/tests/test_morning_briefing.py` | 14 | `tests._testdb` |
| `gateway/tests/test_newsletter_blast_bounding.py` | 39 | `tests._testdb` |
| `gateway/tests/test_onboarding_routes.py` | 26 | `tests._testdb` |
| `gateway/tests/test_onboarding_tour.py` | 36 | `tests._testdb` |
| `gateway/tests/test_password_reset.py` | 18 | `tests._testdb` |
| `gateway/tests/test_portfolio_sync.py` | 35 | `tests._testdb` |
| `gateway/tests/test_profile.py` | 28 | `tests._testdb` |
| `gateway/tests/test_query_perf.py` | 15 | `tests._testdb` |
| `gateway/tests/test_referrals.py` | 31 | `tests._testdb` |
| `gateway/tests/test_resolution_polling.py` | 17 | `tests._testdb` |
| `gateway/tests/test_scenarios.py` | 24 | `tests._testdb` |
| `gateway/tests/test_sentry.py` | 5 | `importlib` |
| `gateway/tests/test_seo.py` | 6 | `re` |
| `gateway/tests/test_sessions_management.py` | 20 | `tests._testdb` |
| `gateway/tests/test_settings_billing.py` | 29 | `html` |
| `gateway/tests/test_sharing.py` | 30 | `tests._testdb` |
| `gateway/tests/test_status_admin.py` | 22 | `tests._testdb` |
| `gateway/tests/test_status_monitoring.py` | 17 | `tests._testdb` |
| `gateway/tests/test_status_page.py` | 17 | `tests._testdb` |
| `gateway/tests/test_subproducts.py` | 22 | `types.SimpleNamespace` |
| `gateway/tests/test_user_predictions.py` | 25 | `tests._testdb` |
| `gateway/tests/test_waitlist.py` | 8 | `tests._testdb` |
| `gateway/tests/test_webhooks.py` | 27 | `tests._testdb` |
| `gateway/tests/test_weekly_digest.py` | 8 | `tests._testdb` |
| `gateway/user_prediction_routes.py` | 340 | `html` |

## Reproduction

```bash
cd /Users/shocakarel/Habbig
python3 -m pyflakes gateway/
```

Exit code is `1` when findings exist. Output goes to stdout.

## Follow-up

- [ ] Verify each `__init__.py` finding — are the imports re-exported as the package's public API?
- [ ] Triage the 5 undefined names — these are likely real bugs.
- [ ] Sweep one-off unused imports in `queries/*.py` and `*_routes.py` (low risk, mechanical).
- [ ] Decide whether to add `# noqa: F401` to the `__init__.py` re-exports for cleaner future runs.
- [ ] Consider running `ruff check --select F gateway/` for the same findings with `# noqa` honored.

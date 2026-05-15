# OpenAPI Duplicate Operation ID Audit

Generated: 2026-05-15

## Method

Imported the FastAPI app (`gateway.server.app`), enumerated `app.routes`, and
captured FastAPI's own `Duplicate Operation ID ...` warnings emitted during
`app.openapi()` schema generation. The warnings are what `test_api_docs`
surfaces, and they fire when FastAPI auto-derives an `operation_id` from
`name + path-flatten` and finds the same string already in the schema. Only
the first occurrence remains in the generated OpenAPI document; the rest are
silently dropped.

No `operation_id=` is set explicitly on any route in the app (0 of 626).
Every duplicate below is therefore an auto-id collision driven by a
double-registration of the same endpoint or by an unrelated route reusing
the same function name + path shape.

## Summary

| Metric                              | Count |
| ----------------------------------- | ----- |
| Total routes on `app`               | 626   |
| Paths in generated OpenAPI schema   | 245   |
| Operations in generated schema      | 275   |
| FastAPI duplicate-id warnings       | 105   |
| Unique duplicate operation IDs      | 104   |
| Cross-module collisions             | 4     |
| Intra-module double-registrations   | 100   |

Of the 104 unique colliding operation IDs:

- **4** are real cross-module name reuse (two different endpoints map to the
  same auto-generated `operation_id` because their function names + paths
  collapse to the same string).
- **100** are the same handler function being attached to the same path on
  `app` twice — strong indicator that a router is being included more than
  once, or that legacy duplicates in `server_features.py` shadow the modular
  router files.

## Top 5 collisions (by registration count)

| Rank | Operation ID                                              | Routes | Modules                                              |
| ---- | --------------------------------------------------------- | ------ | ---------------------------------------------------- |
| 1    | `api_newsletter_position_api_newsletter_position_get`     | 3      | `public_routes`, `server_features` (x2)              |
| 2    | `api_list_saved_api_saved_get`                            | 3      | `scenarios_routes`, `server_features` (x2)           |
| 3    | `api_update_follow_api_sources__handle__follow_patch`     | 3      | `server_features` (x2), `collections_routes`         |
| 4    | `api_kelly_calculate_api_kelly_calculate_post`            | 2      | `market_routes`, `portfolio.routes`                  |
| 5    | `terms_page_terms_get`                                    | 2      | `server_features` (x2)                               |

(Rank 5 is illustrative — there are 99 other intra-`server_features` /
intra-module pairs with exactly count=2; they collide on the same module
re-including the same handler.)

## Cross-module collisions (need real disambiguation)

These four are not pure double-registration — distinct functions or paths
ended up with the same auto-id and they will need explicit `operation_id=`
or unique function/path names.

### 1. `api_newsletter_position_api_newsletter_position_get` (3 registrations)
- `GET /api/newsletter/position` &mdash; `public_routes`
- `GET /api/newsletter/position` &mdash; `server_features`
- `GET /api/newsletter/position` &mdash; `server_features`

### 2. `api_list_saved_api_saved_get` (3 registrations)
- `GET /api/scenario/saved` &mdash; `scenarios_routes` (function `api_list_saved` reused for a different endpoint)
- `GET /api/saved` &mdash; `server_features`
- `GET /api/saved` &mdash; `server_features`

### 3. `api_update_follow_api_sources__handle__follow_patch` (3 registrations)
- `PATCH /api/sources/{handle}/follow` &mdash; `server_features`
- `PATCH /api/sources/{handle}/follow` &mdash; `server_features`
- `PATCH /api/collections/{id}/follow` &mdash; `collections_routes` (function `api_update_follow` reused for a different endpoint)

### 4. `api_kelly_calculate_api_kelly_calculate_post` (2 registrations)
- `POST /api/kelly/calculate` &mdash; `market_routes`
- `POST /api/kelly/calculate` &mdash; `portfolio.routes`

## Intra-module double-registrations (router included twice)

Counts of duplicated endpoints per module. All entries in each module pair
share the same path and same handler — diagnostic of `app.include_router(...)`
being called more than once for the router, or of `server_features.py`
legacy endpoints being shadowed by a modular router that re-registers them.

| Module                      | Duplicated endpoints |
| --------------------------- | -------------------- |
| `server_features`           | 39                   |
| `take_routes`               | 12                   |
| `affiliate_routes`          | 11                   |
| `admin_jobs_routes`         | 7                    |
| `embed_routes`              | 6                    |
| `forecast_routes`           | 5                    |
| `push_routes`               | 5                    |
| `admin_emails_routes`       | 4                    |
| `admin_cost_alerts_routes`  | 3                    |
| `admin_test_emails_routes`  | 3                    |
| `admin_integrations_routes` | 3                    |
| `admin_health_monitor_routes` | 2                  |
| **Total**                   | **100**              |

### `server_features` (39)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/terms` | `terms_page` |
| GET    | `/privacy` | `privacy_page` |
| GET    | `/dpa` | `dpa_page` |
| GET    | `/unsubscribe` | `unsubscribe_page` |
| POST   | `/api/notifications/email-preferences` | `api_email_preferences` |
| POST   | `/api/set-language` | `api_set_language` |
| POST   | `/auth/forgot-password` | `auth_forgot_password` |
| POST   | `/auth/reset-password` | `auth_reset_password` |
| POST   | `/api/newsletter` | `api_newsletter_v2` |
| POST   | `/api/account/delete` | `api_account_delete` |
| POST   | `/api/account/delete/cancel` | `api_account_delete_cancel` |
| GET    | `/sources/{handle}` | `public_source_profile` |
| GET    | `/sitemap.xml` | `sitemap_xml` |
| GET    | `/robots.txt` | `robots_txt` |
| POST   | `/api/markets/{market_slug}/track-view` | `api_track_market_view` |
| POST   | `/admin/markets/{market_slug}/mark-resolved` | `admin_mark_market_resolved` |
| POST   | `/admin/jobs/weekly-digest/run` | `admin_run_weekly_digest` |
| GET    | `/admin/api/jobs/status` | `admin_jobs_status` |
| GET    | `/admin/api/jobs/recent` | `admin_jobs_recent` |
| POST   | `/admin/api/jobs/{job_id}/retry` | `admin_jobs_retry` |
| GET    | `/api/search` | `api_search` |
| POST   | `/api/saved/{prediction_id}` | `api_save_prediction` |
| DELETE | `/api/saved/{prediction_id}` | `api_unsave_prediction` |
| PATCH  | `/api/saved/{prediction_id}` | `api_update_saved_notes` |
| GET    | `/saved` | `saved_page` |
| POST   | `/api/sources/{handle}/follow` | `api_follow_source` |
| DELETE | `/api/sources/{handle}/follow` | `api_unfollow_source` |
| GET    | `/api/sources/following` | `api_list_following` |
| GET    | `/api/markets/{slug:path}/chart` | `api_market_chart` |
| POST   | `/api/markets/{slug:path}/snapshot` | `api_ingest_market_snapshot` |
| GET    | `/token` | `token_page` |
| POST   | `/auth/validate-token` | `auth_validate_token` |
| GET    | `/register` | `register_page` |
| POST   | `/auth/register` | `auth_register` |
| POST   | `/auth/login` | `auth_login` |
| POST   | `/auth/logout` | `auth_logout` |
| GET    | `/api/auth/sessions` | `api_auth_sessions_list` |
| DELETE | `/api/auth/sessions/{session_id}` | `api_auth_sessions_revoke` |
| DELETE | `/api/auth/sessions` | `api_auth_sessions_revoke_all` |

### `take_routes` (12)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/api/v1/markets/{slug}/takes` | `api_list_takes` |
| POST   | `/api/v1/markets/{slug}/takes` | `api_create_take` |
| PATCH  | `/api/v1/takes/{take_id}` | `api_update_take` |
| DELETE | `/api/v1/takes/{take_id}` | `api_delete_take` |
| POST   | `/api/v1/takes/{take_id}/vote` | `api_vote_on_take` |
| DELETE | `/api/v1/takes/{take_id}/vote` | `api_clear_vote` |
| POST   | `/api/v1/takes/{take_id}/report` | `api_report_take` |
| GET    | `/settings/takes` | `settings_takes_page` |
| GET    | `/admin/moderation` | `admin_moderation_page` |
| POST   | `/api/v1/admin/takes/{take_id}/delete` | `api_admin_delete_take` |
| POST   | `/api/v1/admin/reports/{report_id}/resolve` | `api_admin_resolve_report` |
| GET    | `/u/{user_id}/takes` | `public_user_takes_page` |

### `affiliate_routes` (11)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/partner/{code}` | `partner_click` |
| GET    | `/p/{code}` | `partner_click_short` |
| GET    | `/settings/affiliate` | `affiliate_dashboard` |
| GET    | `/api/v1/affiliate` | `api_affiliate_info` |
| POST   | `/api/v1/affiliate/links` | `api_affiliate_create_link` |
| GET    | `/api/v1/affiliate/conversions` | `api_affiliate_conversions` |
| POST   | `/api/v1/affiliate/payout-request` | `api_affiliate_payout_request` |
| GET    | `/admin/affiliates` | `admin_affiliates_list` |
| POST   | `/admin/affiliates` | `admin_affiliates_create` |
| PATCH  | `/admin/affiliates/{affiliate_id}` | `admin_affiliates_update` |
| POST   | `/admin/affiliates/{affiliate_id}/payout` | `admin_affiliates_mark_paid` |

### `admin_jobs_routes` (7)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/admin/api/jobs/refresh` | `admin_api_jobs_refresh` |
| GET    | `/admin/api/jobs` | `admin_api_jobs` |
| GET    | `/admin/api/jobs/{name}/history` | `admin_api_job_history` |
| POST   | `/admin/api/jobs/{name}/pause` | `admin_api_job_pause` |
| POST   | `/admin/api/jobs/{name}/resume` | `admin_api_job_resume` |
| POST   | `/admin/api/jobs/{name}/trigger` | `admin_api_job_trigger` |
| GET    | `/admin/jobs` | `admin_jobs_page` |

### `embed_routes` (6)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/settings/embeds` | `settings_embeds_page` |
| GET    | `/api/embeds` | `api_list_embeds` |
| POST   | `/api/embeds` | `api_create_embed` |
| DELETE | `/api/embeds/{widget_id}` | `api_deactivate_embed` |
| POST   | `/api/embeds/{widget_id}/rotate-token` | `api_rotate_embed_token` |
| GET    | `/embed/{widget_id}` | `serve_embed` |

### `forecast_routes` (5)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/api/v1/forecasts/providers` | `api_forecasts_providers` |
| GET    | `/api/v1/forecasts/compare/{market_slug}` | `api_forecasts_compare` |
| GET    | `/dashboard/models` | `dashboard_models` |
| GET    | `/admin/equivalences` | `admin_equivalences` |
| POST   | `/admin/equivalences/{market_slug}/{provider}` | `admin_equivalence_action` |

### `push_routes` (5)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/api/push/vapid-key` | `api_push_vapid_key` |
| POST   | `/api/push/subscribe` | `api_push_subscribe` |
| POST   | `/api/push/unsubscribe` | `api_push_unsubscribe` |
| POST   | `/api/push/test` | `api_push_test` |
| GET    | `/api/push/subscriptions` | `api_push_subscriptions` |

### `admin_emails_routes` (4)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/admin/emails` | `admin_emails_page` |
| GET    | `/admin/api/emails` | `admin_api_emails_list` |
| GET    | `/admin/emails/{email_id}` | `admin_email_detail` |
| POST   | `/admin/emails/{email_id}/resend` | `admin_email_resend` |

### `admin_cost_alerts_routes` (3)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/admin/api/ai-cost/refresh` | `admin_api_ai_cost_refresh` |
| POST   | `/admin/ai-cost/kill-switch` | `admin_ai_cost_kill_switch` |
| GET    | `/admin/cost-alerts` | `admin_cost_alerts_page` |

### `admin_test_emails_routes` (3)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/admin/test-emails` | `admin_test_emails_page` |
| GET    | `/admin/test-emails/preview/{template_name}` | `admin_test_emails_preview` |
| POST   | `/admin/test-emails/send` | `admin_test_emails_send` |

### `admin_integrations_routes` (3)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/api/admin/integrations` | `admin_integrations_api` |
| POST   | `/api/admin/integrations/{slug}/test` | `admin_integrations_test` |
| GET    | `/admin/integrations` | `admin_integrations_page` |

### `admin_health_monitor_routes` (2)

| Method | Path | Handler |
| ------ | ---- | ------- |
| GET    | `/api/admin/health-monitor` | `admin_health_monitor_api` |
| GET    | `/admin/health-monitor` | `admin_health_monitor_page` |

## Likely root causes

1. **Routers included twice.** 100 of 104 duplicates are
   `(same module, same handler, same path)` registered more than once on
   `app`. The most plausible mechanic: `app.include_router(...)` called
   twice for the same router (e.g. once directly in `server.py` and once
   transitively via another module that also calls
   `app.include_router(take_routes.router)`), or a router being mounted
   both with and without a prefix.
2. **Legacy shadowing in `server_features.py`.** 39 endpoints in
   `server_features.py` collide with handlers of the same name elsewhere.
   This module looks like a historical catch-all that the modular routers
   (`auth/*`, `take_routes`, etc.) were meant to replace; both are wired
   into `app` today.
3. **Genuine name reuse across unrelated endpoints.** The 4 cross-module
   collisions need explicit `operation_id=` strings or renamed handler
   functions:
   - `api_list_saved` reused in `scenarios_routes` for `/api/scenario/saved`
     while `server_features` owns `/api/saved`.
   - `api_update_follow` reused in `collections_routes` for
     `/api/collections/{id}/follow` while `server_features` owns
     `/api/sources/{handle}/follow`.
   - `api_newsletter_position` registered by both `public_routes` and
     `server_features` for the same path.
   - `api_kelly_calculate` registered by both `market_routes` and
     `portfolio.routes` for the same path.

## Reproduction

```python
import warnings
from gateway import server

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    server.app.openapi()
    dups = [str(w.message) for w in caught
            if "Duplicate Operation ID" in str(w.message)]

print(f"FastAPI duplicate-id warnings: {len(dups)}")  # -> 105
```

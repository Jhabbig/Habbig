# Embed Widgets Test Results

**Date:** 2026-05-15
**Command:**
```bash
python3 -m pytest gateway/tests/test_embed*.py -q -p no:logging 2>&1 | tail -30
```

## Summary

- **Passed:** 11
- **Failed:** 29
- **Total:** 40
- **Duration:** ~20s

## Files exercised

- `gateway/tests/test_embed_widgets.py` — only file matched by glob.

## Output (tail -30)

```
=========================== short test summary info ============================
FAILED gateway/tests/test_embed_widgets.py::TestAuthGates::test_create_happy_path
FAILED gateway/tests/test_embed_widgets.py::TestAuthGates::test_create_requires_active_subscription
FAILED gateway/tests/test_embed_widgets.py::TestAuthGates::test_list_returns_user_widgets_with_stats
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_bad_domain_rejects_scheme
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_bad_domain_rejects_single_label
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_bad_theme - ...
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_bad_widget_type
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_best_bets_ignores_target
FAILED gateway/tests/test_embed_widgets.py::TestValidation::test_missing_target_for_source
FAILED gateway/tests/test_embed_widgets.py::TestLimit::test_deactivating_frees_a_slot
FAILED gateway/tests/test_embed_widgets.py::TestLimit::test_eleventh_widget_rejected
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_correct_domain_referer_accepted
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_deactivated_widget_returns_error
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_frame_ancestors_set_per_widget
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_no_referer_rejected_when_domain_configured
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_token_validation_accepts_real_token
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_token_validation_rejects_bad_token
FAILED gateway/tests/test_embed_widgets.py::TestEmbedRendering::test_wrong_domain_referer_rejected
FAILED gateway/tests/test_embed_widgets.py::TestImpressions::test_bad_token_does_not_increment
FAILED gateway/tests/test_embed_widgets.py::TestImpressions::test_impression_increments
FAILED gateway/tests/test_embed_widgets.py::TestRotation::test_rotation_invalidates_old_token
FAILED gateway/tests/test_embed_widgets.py::TestRotation::test_rotation_of_unknown_widget_404
FAILED gateway/tests/test_embed_widgets.py::TestRotation::test_rotation_scoped_to_owner
FAILED gateway/tests/test_embed_widgets.py::TestDeactivation::test_deactivate_is_idempotent
FAILED gateway/tests/test_embed_widgets.py::TestDeactivation::test_deactivate_scoped_to_owner
FAILED gateway/tests/test_embed_widgets.py::TestDeactivation::test_deactivate_unknown_widget_404
FAILED gateway/tests/test_embed_widgets.py::TestSubscriptionLapse::test_lapse_deactivates_all_widgets_on_first_embed_hit
FAILED gateway/tests/test_embed_widgets.py::TestSubscriptionLapse::test_lapsed_user_cannot_create
FAILED gateway/tests/test_embed_widgets.py::TestSettingsPage::test_page_renders_for_authenticated_user
29 failed, 11 passed in 20.60s
```

## Notes

- Root cause of failures: `sqlite3.OperationalError: table sessions has no column named token` in `gateway/queries/auth.py:224` (called from the `_auth_client` fixture in `test_embed_widgets.py:63`).
- The test fixture inserts a session row with a `token` column, but the live `sessions` schema for this test DB lacks that column — schema/migration drift between the embed test fixtures and the rest of the suite.
- The 11 passing tests are the cases that do not exercise the authenticated-client fixture (e.g. unauthenticated rejection paths).

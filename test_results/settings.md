# Settings Tests

Command: `python3 -m pytest gateway/tests/test_settings*.py -q -p no:logging 2>&1 | tail -30`

Date: 2026-05-15

## Summary

- **Passed:** 14
- **Failed:** 26
- **Errors:** 29
- **Duration:** 56.65s

## Output (tail -30)

```
FAILED gateway/tests/test_settings_trading_addon.py::TestConfigPatch::test_patch_validates_max_cap_upper_bound
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_active_user_sees_danger_zone_cancel_button
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_addons_section_shows_both_addons
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_billing_history_table_scaffold_present
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_cancel_modal_and_reason_dropdown_present
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_cancelled_user_sees_resubscribe_banner
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_change_plan_modal_form_posts_to_subscribe
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_data_payload_json_is_parseable
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_fresh_user_sees_no_plan_empty_state
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_payment_method_section_present
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_pro_user_sees_current_plan_block
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_pro_user_sees_pro_features
ERROR gateway/tests/test_settings_billing.py::TestSettingsBillingPage::test_trader_user_sees_upgrade_cta
ERROR gateway/tests/test_settings_billing.py::TestInvoicesEndpoint::test_fresh_user_has_empty_invoices
ERROR gateway/tests/test_settings_billing.py::TestInvoicesEndpoint::test_pagination_cursor
ERROR gateway/tests/test_settings_billing.py::TestInvoicesEndpoint::test_pro_user_invoices_shape
ERROR gateway/tests/test_settings_billing.py::TestInvoicesEndpoint::test_pro_user_sees_addon_invoice
ERROR gateway/tests/test_settings_billing.py::TestInvoicesEndpoint::test_unauth_returns_401
ERROR gateway/tests/test_settings_billing.py::TestInvoicePdfStub::test_pdf_returns_501
ERROR gateway/tests/test_settings_billing.py::TestInvoicePdfStub::test_pdf_unauth_returns_401
ERROR gateway/tests/test_settings_billing.py::TestPortalStub::test_portal_redirects_to_enquire
ERROR gateway/tests/test_settings_billing.py::TestPortalStub::test_portal_unauth_redirects_to_token
ERROR gateway/tests/test_settings_billing.py::TestCancelFlow::test_cancel_sets_status_cancelled
ERROR gateway/tests/test_settings_billing.py::TestCancelFlow::test_cancel_unauth_redirects
ERROR gateway/tests/test_settings_billing.py::TestCancelFlow::test_flash_banner_appears_after_cancel
ERROR gateway/tests/test_settings_billing.py::TestResubscribeFlow::test_resubscribe_does_not_reactivate_expired_sub
ERROR gateway/tests/test_settings_billing.py::TestResubscribeFlow::test_resubscribe_flips_status_back_to_active
ERROR gateway/tests/test_settings_billing.py::TestAddonFlow::test_add_trading_addon_requires_stripe_checkout
ERROR gateway/tests/test_settings_billing.py::TestAddonFlow::test_cancel_trading_addon
ERROR gateway/tests/test_settings_billing.py::TestAddonFlow::test_unknown_addon_is_noop_redirect
```

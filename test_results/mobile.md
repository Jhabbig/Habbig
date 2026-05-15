# Mobile Viewport Test Results

**Date:** 2026-05-15
**Command:** `python3 -m pytest gateway/tests/test_mobile*.py --tb=line -q -p no:logging`

## Summary

- **Passed:** 11
- **Skipped:** 1
- **Failed:** 0
- **Total:** 12
- **Duration:** ~0.5s

## Tests

### gateway/tests/test_mobile_viewport.py

| Test | Result |
|---|---|
| `TestMobileCSS::test_bottom_sheet_pattern` | PASS |
| `TestMobileCSS::test_hamburger_class_styled` | PASS |
| `TestMobileCSS::test_input_min_font_size` | PASS |
| `TestMobileCSS::test_safe_area_inset_bottom` | PASS |
| `TestMobileCSS::test_sidebar_drawer_translate` | PASS |
| `TestMobileCSS::test_table_wrap_pattern` | PASS |
| `TestMobileCSS::test_tap_target_min_44` | PASS |
| `TestPWAMiddlewareInjects::test_backdrop_injected` | PASS |
| `TestPWAMiddlewareInjects::test_hamburger_button_injected` | PASS |
| `TestNarveAppDrawerWiring::test_init_sidebar_drawer_present` | PASS |
| `TestHTMLTablesWrapped::test_each_page_wraps_every_table` | PASS |
| `TestNoHorizontalScroll::test_no_hscroll_at_375` | SKIP |

## Raw output

```
...........s                                                             [100%]
11 passed, 1 skipped in 0.50s
```

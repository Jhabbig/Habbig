# Design audit — `gateway/static/pages/*.css`

**Date**: 2026-05-15  
**Scope**: 82 CSS files under `/Users/shocakarel/Habbig/gateway/static/pages/`  
**Standard**: narve-design skill — monochrome only, three typefaces (Inter / Geist Mono / Instrument Serif via `var(--font-ui|mono|display)`), tokens not hardcoded values, no inline `<style>` references, no `@import` of remote CDN URLs.  
**Token source**: `/Users/shocakarel/Habbig/gateway/static/tokens.css`

---

## Checks performed

Each file scanned line-by-line (comments stripped). A line is flagged when a property uses a raw literal instead of the matching token:

| Check | Rule |
|---|---|
| color_hardcoded | Any `#rgb`, `#rrggbb`, `rgb()`, `rgba()`, `hsl()`, or non-neutral named colour anywhere in a declaration value. narve allows only greys/black/white, and they must come from `--bg-*` / `--text-*` / `--border-*` / `--interactive-*` tokens. |
| spacing_hardcoded | Raw `px`/`rem`/`em` in `padding`, `margin`, `gap`, `top/left/right/bottom`, `width/height`, `border-width`, `border-radius`. Should use `--space-1..10`, `--row-pad-*`, `--card-pad`, `--page-pad`, etc. |
| radius_hardcoded | Subset of spacing — raw `px` in any `border-radius*`. Should be `--radius-{xs,sm,md,lg,xl,full}`. |
| font_size_hardcoded | Raw `px` in `font-size` with no `var()`. Should use `--text-{xs..5xl}`. |
| duration_hardcoded | Raw `s`/`ms` in `transition`/`animation` without `var(--duration-*)`. Allowed durations: fast (0.12s), base (0.2s), slow (0.4s). |
| shadow_hardcoded | `box-shadow` literal not via `var(--shadow-{sm,md,lg})`. |
| font_family_not_tokenized | `font-family` not using `var(--font-ui|body|display|mono)` (or `inherit`). |
| import_http | `@import url(http…)` — CDN external (banned: fonts must be self-hosted via `/static/fonts/`). |
| inline_style_ref | Literal `<style` substring (informational — these are comments in our case, but reported). |
| z_index_hardcoded | Numeric `z-index` not via `var(--z-*)`. |

> **Note**: when a single declaration mixes a token *and* a hardcoded value (e.g. `padding: var(--space-3) 14px`), the literal is still flagged. The audit is strict per narve-design: "If a needed token doesn't exist, add it to `tokens.css` rather than hardcoding."

---

## Top 5 worst files (by total violations)

| Rank | File | Total | Worst category |
|---|---|---|---|
| 1 | `settings_billing.css` | **96** | `spacing_hardcoded` (75) |
| 2 | `landing.css` | **88** | `spacing_hardcoded` (69) |
| 3 | `admin-shell.css` | **81** | `spacing_hardcoded` (54) |
| 4 | `subscribe.css` | **62** | `spacing_hardcoded` (51) |
| 5 | `prerelease.css` | **60** | `spacing_hardcoded` (34) |

---

## Headline findings

| Category | Total across all files |
|---|---|
| `color_hardcoded` | 94 |
| `spacing_hardcoded` | 1432 |
| `radius_hardcoded` | 44 |
| `font_size_hardcoded` | 319 |
| `duration_hardcoded` | 79 |
| `shadow_hardcoded` | 7 |
| `font_family_not_tokenized` | 40 |
| `import_http` | 0 |
| `inline_style_ref` | 0 |
| `z_index_hardcoded` | 13 |

---

## Per-file summary table

Columns:

- **C** = colour hardcoded
- **Sp** = spacing hardcoded (raw px/rem/em)
- **R** = radius hardcoded (subset of Sp)
- **Fs** = font-size hardcoded
- **Dur** = duration hardcoded
- **Sh** = shadow hardcoded
- **FF** = font-family not tokenized
- **Imp** = `@import http(s)`
- **Inl** = `<style` literal
- **Z** = z-index hardcoded
- **Tot** = total (Sp already includes R; R column is shown for visibility only and not double-counted)

| File | Bytes | C | Sp | (R) | Fs | Dur | Sh | FF | Imp | Inl | Z | **Tot** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `403.css` | 2082 | 0 | 16 | (0) | 3 | 1 | 0 | 0 | 0 | 0 | 0 | **20** |
| `about.css` | 4636 | 0 | 1 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **2** |
| `account.css` | 4802 | 0 | 30 | (0) | 2 | 2 | 0 | 0 | 0 | 0 | 0 | **34** |
| `admin-churn.css` | 2071 | 0 | 11 | (1) | 2 | 0 | 0 | 1 | 0 | 0 | 0 | **14** |
| `admin-email-edit.css` | 1726 | 2 | 8 | (0) | 3 | 0 | 0 | 2 | 0 | 0 | 0 | **15** |
| `admin-feedback.css` | 2205 | 0 | 11 | (1) | 3 | 1 | 0 | 0 | 0 | 0 | 0 | **15** |
| `admin-sharing.css` | 3028 | 0 | 13 | (0) | 2 | 0 | 0 | 1 | 0 | 0 | 0 | **16** |
| `admin-shell.css` | 16731 | 1 | 54 | (7) | 19 | 4 | 1 | 0 | 0 | 0 | 2 | **81** |
| `admin.css` | 4287 | 0 | 25 | (2) | 5 | 4 | 0 | 1 | 0 | 0 | 0 | **35** |
| `admin_security_bulk.css` | 1764 | 2 | 9 | (0) | 1 | 0 | 0 | 1 | 0 | 0 | 0 | **13** |
| `admin_security_forensics.css` | 2162 | 1 | 13 | (0) | 1 | 0 | 0 | 2 | 0 | 0 | 0 | **17** |
| `admin_status.css` | 3197 | 0 | 16 | (0) | 2 | 0 | 0 | 0 | 0 | 0 | 0 | **18** |
| `admin_webhooks.css` | 1828 | 5 | 11 | (2) | 4 | 0 | 0 | 2 | 0 | 0 | 0 | **22** |
| `ai_usage.css` | 6405 | 0 | 19 | (2) | 13 | 0 | 0 | 0 | 0 | 0 | 0 | **32** |
| `api_docs.css` | 13886 | 0 | 13 | (0) | 4 | 0 | 0 | 0 | 0 | 0 | 0 | **17** |
| `audit_log.css` | 12723 | 0 | 20 | (1) | 12 | 1 | 0 | 0 | 0 | 0 | 0 | **33** |
| `auth.css` | 10906 | 0 | 20 | (0) | 5 | 0 | 0 | 0 | 0 | 0 | 0 | **25** |
| `billing.css` | 2410 | 0 | 12 | (0) | 2 | 1 | 0 | 0 | 0 | 0 | 0 | **15** |
| `calendar.css` | 4134 | 0 | 30 | (1) | 5 | 1 | 0 | 0 | 0 | 0 | 0 | **36** |
| `card_preview.css` | 3478 | 3 | 24 | (0) | 3 | 0 | 0 | 0 | 0 | 0 | 0 | **30** |
| `changelog.css` | 14903 | 0 | 8 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **9** |
| `contact.css` | 6145 | 0 | 4 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **5** |
| `dashboards.css` | 6890 | 0 | 5 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **6** |
| `enquire.css` | 7120 | 0 | 45 | (0) | 6 | 7 | 0 | 0 | 0 | 0 | 0 | **58** |
| `error_page.css` | 5342 | 0 | 3 | (0) | 1 | 1 | 0 | 0 | 0 | 0 | 0 | **5** |
| `faq.css` | 3780 | 0 | 2 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **3** |
| `feedback-detail.css` | 2235 | 0 | 14 | (1) | 2 | 0 | 0 | 0 | 0 | 0 | 0 | **16** |
| `feedback.css` | 2182 | 0 | 12 | (1) | 2 | 1 | 0 | 0 | 0 | 0 | 0 | **15** |
| `feeds.css` | 10986 | 0 | 19 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **20** |
| `forgot-password.css` | 1904 | 0 | 14 | (0) | 1 | 3 | 0 | 0 | 0 | 0 | 0 | **18** |
| `gate.css` | 2856 | 1 | 14 | (0) | 5 | 1 | 1 | 0 | 0 | 0 | 1 | **23** |
| `how_it_works.css` | 5072 | 0 | 3 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **4** |
| `impressum.css` | 3578 | 0 | 27 | (2) | 0 | 1 | 0 | 0 | 0 | 0 | 0 | **28** |
| `intelligence.css` | 5309 | 0 | 34 | (2) | 7 | 1 | 0 | 0 | 0 | 0 | 0 | **42** |
| `invite.css` | 845 | 0 | 6 | (1) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **7** |
| `invite_public.css` | 3571 | 5 | 21 | (1) | 2 | 0 | 0 | 0 | 0 | 0 | 0 | **28** |
| `invites_settings.css` | 3167 | 0 | 19 | (0) | 3 | 0 | 0 | 2 | 0 | 0 | 0 | **24** |
| `landing.css` | 10631 | 2 | 69 | (1) | 11 | 5 | 1 | 0 | 0 | 0 | 0 | **88** |
| `leaderboard.css` | 2859 | 0 | 16 | (0) | 2 | 0 | 0 | 0 | 0 | 0 | 0 | **18** |
| `legal.css` | 7117 | 13 | 2 | (0) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **15** |
| `methodology.css` | 5324 | 0 | 2 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 1 | **4** |
| `narve-brand.css` | 1246 | 0 | 9 | (0) | 1 | 0 | 0 | 1 | 0 | 0 | 0 | **11** |
| `onboarding.css` | 6438 | 0 | 43 | (1) | 5 | 3 | 0 | 0 | 0 | 0 | 0 | **51** |
| `poster.css` | 3570 | 10 | 15 | (1) | 6 | 7 | 0 | 7 | 0 | 0 | 2 | **47** |
| `prerelease.css` | 6770 | 0 | 34 | (2) | 16 | 7 | 0 | 1 | 0 | 0 | 2 | **60** |
| `press.css` | 1827 | 1 | 14 | (1) | 2 | 0 | 0 | 1 | 0 | 0 | 0 | **18** |
| `preview.css` | 7317 | 1 | 13 | (0) | 16 | 0 | 1 | 0 | 0 | 0 | 0 | **31** |
| `pricing.css` | 16505 | 0 | 17 | (0) | 8 | 0 | 0 | 0 | 0 | 0 | 0 | **25** |
| `profile-public.css` | 10025 | 0 | 8 | (0) | 0 | 0 | 0 | 0 | 0 | 0 | 1 | **9** |
| `profile.css` | 1970 | 0 | 11 | (0) | 2 | 0 | 0 | 1 | 0 | 0 | 0 | **14** |
| `realtime-admin.css` | 3434 | 4 | 25 | (2) | 7 | 1 | 1 | 1 | 0 | 0 | 0 | **39** |
| `referrals.css` | 3963 | 1 | 17 | (0) | 4 | 1 | 0 | 0 | 0 | 0 | 0 | **23** |
| `reset-password.css` | 2460 | 0 | 15 | (0) | 1 | 3 | 0 | 0 | 0 | 0 | 0 | **19** |
| `settings-profile.css` | 1920 | 0 | 14 | (0) | 3 | 0 | 0 | 2 | 0 | 0 | 0 | **19** |
| `settings.css` | 559 | 0 | 0 | (0) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `settings_api_key_reveal.css` | 3282 | 8 | 20 | (1) | 9 | 0 | 0 | 0 | 0 | 0 | 0 | **37** |
| `settings_api_keys.css` | 7389 | 14 | 31 | (2) | 15 | 0 | 0 | 0 | 0 | 0 | 0 | **60** |
| `settings_billing.css` | 13886 | 4 | 75 | (2) | 13 | 2 | 1 | 0 | 0 | 0 | 1 | **96** |
| `settings_billing_cancel.css` | 1923 | 0 | 7 | (0) | 1 | 1 | 0 | 0 | 0 | 0 | 0 | **9** |
| `settings_embeds.css` | 6522 | 1 | 35 | (0) | 6 | 1 | 0 | 4 | 0 | 0 | 1 | **48** |
| `settings_integrations.css` | 4946 | 1 | 8 | (0) | 1 | 0 | 0 | 2 | 0 | 0 | 0 | **12** |
| `settings_offline.css` | 4617 | 0 | 22 | (0) | 6 | 2 | 0 | 0 | 0 | 0 | 0 | **30** |
| `settings_redesign.css` | 8571 | 0 | 2 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **3** |
| `settings_saved_views.css` | 3237 | 0 | 14 | (0) | 1 | 1 | 0 | 1 | 0 | 0 | 0 | **17** |
| `settings_trading_addon.css` | 5543 | 1 | 24 | (0) | 1 | 0 | 0 | 1 | 0 | 0 | 0 | **27** |
| `settings_webhooks.css` | 3884 | 9 | 28 | (2) | 10 | 0 | 0 | 3 | 0 | 0 | 0 | **50** |
| `shared_invalid.css` | 1293 | 0 | 6 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **7** |
| `shared_market.css` | 2047 | 0 | 10 | (0) | 3 | 0 | 0 | 0 | 0 | 0 | 0 | **13** |
| `shared_prediction.css` | 1846 | 0 | 9 | (0) | 3 | 0 | 0 | 0 | 0 | 0 | 0 | **12** |
| `shared_source.css` | 2116 | 0 | 12 | (0) | 4 | 0 | 0 | 0 | 0 | 0 | 0 | **16** |
| `signal-search.css` | 20698 | 1 | 21 | (0) | 6 | 0 | 0 | 0 | 0 | 0 | 0 | **28** |
| `signup.css` | 2161 | 0 | 14 | (0) | 1 | 3 | 0 | 0 | 0 | 0 | 0 | **18** |
| `source.css` | 8884 | 0 | 7 | (0) | 0 | 0 | 0 | 0 | 0 | 0 | 1 | **8** |
| `sources.css` | 10105 | 0 | 14 | (0) | 2 | 0 | 0 | 0 | 0 | 0 | 1 | **17** |
| `status.css` | 8944 | 0 | 13 | (1) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **14** |
| `subproduct_landing.css` | 17615 | 0 | 21 | (0) | 4 | 7 | 0 | 0 | 0 | 0 | 0 | **32** |
| `subscribe.css` | 6218 | 1 | 51 | (0) | 5 | 4 | 1 | 0 | 0 | 0 | 0 | **62** |
| `support.css` | 4193 | 0 | 3 | (0) | 1 | 0 | 0 | 0 | 0 | 0 | 0 | **4** |
| `suspended.css` | 1783 | 0 | 14 | (0) | 3 | 1 | 0 | 0 | 0 | 0 | 0 | **18** |
| `team.css` | 2173 | 0 | 19 | (2) | 2 | 0 | 0 | 1 | 0 | 0 | 0 | **22** |
| `user_prediction_detail.css` | 1436 | 2 | 8 | (0) | 1 | 0 | 0 | 1 | 0 | 0 | 0 | **12** |
| `user_prediction_profile.css` | 1961 | 0 | 14 | (1) | 5 | 0 | 0 | 1 | 0 | 0 | 0 | **20** |

---

## Top 5 — detail

### 1. `settings_billing.css` — 96 violations

Path: `/Users/shocakarel/Habbig/gateway/static/pages/settings_billing.css`  
Size: 13886 bytes

Breakdown: `spacing_hardcoded`=75, `font_size_hardcoded`=13, `color_hardcoded`=4, `duration_hardcoded`=2, `radius_hardcoded`=2, `shadow_hardcoded`=1, `z_index_hardcoded`=1

**color hardcoded (first 10):**

- L106: `background: rgba(0, 0, 0, 0.55)` — rgb(...)
- L108: `box-shadow: 0 20px 40px rgba(0,0,0,0.3)` — rgb(...)
- L122: `background: rgba(245, 158, 11, 0.08)` — rgb(...)
- L122: `border: 1px solid rgba(245, 158, 11, 0.25)` — rgb(...)

**spacing hardcoded (first 10):**

- L9: `padding: 24px` — 24px
- L9: `margin-bottom: 20px` — 20px
- L10: `margin-bottom: 4px` — 4px
- L11: `margin-bottom: 16px` — 16px
- L14: `gap: 24px` — 24px
- L15: `gap: 6px` — 6px
- L15: `padding: 4px 10px` — 4px, 10px
- L22: `margin-top: 10px` — 10px
- L23: `margin-top: 4px` — 4px
- L25: `margin-top: 20px` — 20px
- … and 65 more

**font-size hardcoded (first 10):**

- L10: `font-size: 15px`
- L11: `font-size: 12px`
- L48: `font-size: 12px`
- L57: `font-size: 10px`
- L64: `font-size: 15px`
- L65: `font-size: 10px`
- L66: `font-size: 22px`
- L67: `font-size: 12px`
- L68: `font-size: 12px`
- L69: `font-size: 12px`
- … and 3 more

**box-shadow hardcoded (first 5):**

- L108: `box-shadow: 0 20px 40px rgba(0,0,0,0.3)`

**duration hardcoded (first 5):**

- L35: `transition: background 0.15s, border-color 0.15s, transform 0.1s` — 0.15s, 0.15s, 0.1s
- L53: `transition: background 0.15s, color 0.15s` — 0.15s, 0.15s

**z-index hardcoded:**

- L106: `z-index: 200`

### 2. `landing.css` — 88 violations

Path: `/Users/shocakarel/Habbig/gateway/static/pages/landing.css`  
Size: 10631 bytes

Breakdown: `spacing_hardcoded`=69, `font_size_hardcoded`=11, `duration_hardcoded`=5, `color_hardcoded`=2, `shadow_hardcoded`=1, `radius_hardcoded`=1

**color hardcoded (first 10):**

- L170: `box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.08)` — rgb(...)
- L409: `background: rgba(0, 0, 0, 0.6)` — rgb(...)

**spacing hardcoded (first 10):**

- L27: `max-width: 1200px` — 1200px
- L29: `padding: 28px 32px 0` — 28px, 32px
- L38: `gap: 28px` — 28px
- L58: `padding: 9px 18px` — 9px, 18px
- L73: `max-width: 1100px` — 1100px
- L75: `padding: 100px 32px 120px` — 100px, 32px, 120px
- L87: `padding: 6px 14px` — 6px, 14px
- L89: `margin-bottom: 28px` — 28px
- L97: `margin-bottom: 28px` — 28px
- L110: `max-width: 640px` — 640px
- … and 59 more

**font-size hardcoded (first 10):**

- L81: `font-size: 12px`
- L93: `font-size: clamp(40px, 7vw, 76px)`
- L131: `font-size: 15px`
- L148: `font-size: 15px`
- L201: `font-size: 12px`
- L210: `font-size: clamp(28px, 4.5vw, 44px)`
- L265: `font-size: 17px`
- L280: `font-size: 12px`
- L314: `font-size: 17px`
- L434: `font-size: 15px`
- … and 1 more

**box-shadow hardcoded (first 5):**

- L170: `box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.08)`

**duration hardcoded (first 5):**

- L63: `transition: transform 0.1s, box-shadow 0.2s` — 0.1s, 0.2s
- L132: `transition: transform 0.1s, box-shadow 0.2s` — 0.1s, 0.2s
- L238: `transition: transform 0.2s, box-shadow 0.2s` — 0.2s, 0.2s
- L357: `transition: transform 0.1s, box-shadow 0.2s` — 0.1s, 0.2s
- L377: `transition: color 0.15s, border-color 0.15s` — 0.15s, 0.15s

### 3. `admin-shell.css` — 81 violations

Path: `/Users/shocakarel/Habbig/gateway/static/pages/admin-shell.css`  
Size: 16731 bytes

Breakdown: `spacing_hardcoded`=54, `font_size_hardcoded`=19, `radius_hardcoded`=7, `duration_hardcoded`=4, `z_index_hardcoded`=2, `color_hardcoded`=1, `shadow_hardcoded`=1

**color hardcoded (first 10):**

- L314: `box-shadow: 2px 0 12px rgba(0,0,0,0.12)` — rgb(...)

**spacing hardcoded (first 10):**

- L28: `width: 240px` — 240px
- L41: `padding: 4px 12px 24px` — 4px, 12px, 24px
- L42: `margin-bottom: 8px` — 8px
- L48: `gap: 6px` — 6px
- L67: `margin-bottom: 20px` — 20px
- L76: `padding: 4px 12px` — 4px, 12px
- L77: `margin-bottom: 6px` — 6px
- L81: `padding: 7px 12px` — 7px, 12px
- L109: `max-width: 1280px` — 1280px
- L158: `gap: 24px` — 24px
- … and 44 more

**font-size hardcoded (first 10):**

- L55: `font-size: 18px`
- L61: `font-size: 11px`
- L71: `font-size: 10.5px`
- L86: `font-size: 13.5px`
- L165: `font-size: 12px`
- L250: `font-size: 12.5px`
- L272: `font-size: 16px`
- L344: `font-size: 10.5px`
- L361: `font-size: 12px`
- L466: `font-size: 12px`
- … and 9 more

**box-shadow hardcoded (first 5):**

- L314: `box-shadow: 2px 0 12px rgba(0,0,0,0.12)`

**duration hardcoded (first 5):**

- L88: `transition: background 0.12s ease, color 0.12s ease` — 0.12s, 0.12s
- L224: `transition: border-color 0.12s ease, background 0.12s ease` — 0.12s, 0.12s
- L312: `transition: transform 0.2s ease` — 0.2s
- L338: `transition: border-color 0.12s ease` — 0.12s

**z-index hardcoded:**

- L292: `z-index: 101`
- L313: `z-index: 100`

### 4. `subscribe.css` — 62 violations

Path: `/Users/shocakarel/Habbig/gateway/static/pages/subscribe.css`  
Size: 6218 bytes

Breakdown: `spacing_hardcoded`=51, `font_size_hardcoded`=5, `duration_hardcoded`=4, `color_hardcoded`=1, `shadow_hardcoded`=1

**color hardcoded (first 10):**

- L44: `box-shadow: 0 4px 12px rgba(0,0,0,0.3)` — rgb(...)

**spacing hardcoded (first 10):**

- L8: `padding: 32px 24px 80px` — 32px, 24px, 80px
- L9: `max-width: 480px` — 480px
- L10: `gap: 6px` — 6px
- L10: `min-height: 44px` — 44px
- L10: `margin-bottom: 32px` — 32px
- L12: `width: 16px` — 16px
- L12: `height: 16px` — 16px
- L13: `margin-bottom: 8px` — 8px
- L14: `margin-bottom: 32px` — 32px
- L15: `padding: 28px` — 28px
- … and 41 more

**font-size hardcoded (first 10):**

- L13: `font-size: 28px`
- L25: `font-size: 10px`
- L40: `font-size: 15px`
- L43: `font-size: 15px`
- L52: `font-size: 22px`

**box-shadow hardcoded (first 5):**

- L44: `box-shadow: 0 4px 12px rgba(0,0,0,0.3)`

**duration hardcoded (first 5):**

- L19: `transition: border-color 0.2s, box-shadow 0.2s` — 0.2s, 0.2s
- L29: `transition: all 0.2s` — 0.2s
- L34: `transition: border-color 0.15s` — 0.15s
- L43: `transition: transform 0.1s, box-shadow 0.2s` — 0.1s, 0.2s

### 5. `prerelease.css` — 60 violations

Path: `/Users/shocakarel/Habbig/gateway/static/pages/prerelease.css`  
Size: 6770 bytes

Breakdown: `spacing_hardcoded`=34, `font_size_hardcoded`=16, `duration_hardcoded`=7, `radius_hardcoded`=2, `z_index_hardcoded`=2, `font_family_not_tokenized`=1

**font-family not tokenized:**

- L158: `font-family: 'SFMono-Regular', Menlo, Consolas, monospace`

**spacing hardcoded (first 10):**

- L20: `top: 28px` — 28px
- L21: `left: 28px` — 28px
- L24: `gap: 10px` — 10px
- L29: `width: 48px` — 48px
- L29: `height: 48px` — 48px
- L42: `top: 32px` — 32px
- L43: `right: 28px` — 28px
- L55: `padding: 32px` — 32px
- L55: `max-width: 720px` — 720px
- L70: `margin-top: 0.05em` — 0.05em
- … and 24 more

**font-size hardcoded (first 10):**

- L34: `font-size: 1.5rem`
- L45: `font-size: 0.85rem`
- L59: `font-size: clamp(3rem, 7vw, 5.5rem)`
- L66: `font-size: clamp(3rem, 7vw, 5.5rem)`
- L86: `font-size: 1.05rem`
- L109: `font-size: 1rem`
- L126: `font-size: 1rem`
- L133: `font-size: 0.8rem`
- L138: `font-size: 0.85rem`
- L153: `font-size: 0.95rem`
- … and 6 more

**duration hardcoded (first 5):**

- L49: `transition: color 0.2s ease` — 0.2s
- L63: `animation: fadeUp 0.8s ease-out 0.3s both` — 0.8s, 0.3s
- L71: `animation: fadeUp 0.8s ease-out 0.8s both` — 0.8s, 0.8s
- L90: `animation: fadeUp 0.8s ease-out 1.3s both` — 0.8s, 1.3s
- L100: `animation: fadeUp 0.8s ease-out 1.6s both` — 0.8s, 1.6s
- … and 2 more

**z-index hardcoded:**

- L26: `z-index: 10`
- L50: `z-index: 10`

---

## Notes & known caveats

- **`@import http`**: 0 across all files. No CSS file pulls in a remote stylesheet — fonts are self-hosted via `gateway/static/fonts/`. Pass.
- **Inline `<style>` references inside CSS**: 0 once block-comments are excluded. The substring appears in several auto-migration banner comments ("extracted from a small inline `<style>` block by the foundation bundle"), but those are CSS comments, not declarations. The intent of this rule (no `<style>` blocks in templates) belongs to the HTML audit, not this CSS audit. Pass.
- **`var(--name, fallback)` patterns**: fallbacks are stripped before counting. We do NOT flag `padding: var(--space-3, 12px)` even though `12px` literally appears in the source — the fallback is idiomatic safety net for token-cascade failures. We DO still flag mixed-mode lines like `padding: var(--space-3) 14px` (here `14px` lives outside any var() call).
- **`@media print` blocks**: rules like `color: #000` inside `@media print` are flagged as colour violations. They could reasonably be left as raw hex since print engines bind to ink-black, but per a strict reading of "no hardcoded values" they should still use `var(--text-primary)` (which is `#0d0d0d` — close enough to ink-black for print). `legal.css` is the main offender (13 colour hits, all in the print block).
- **`em` on `letter-spacing` / `line-height`**: NOT flagged. `em` is the correct unit there. We only flag `em` on spacing properties (padding/margin/gap/etc).
- **`pt`, `vh`, `vw`, `dvh`, `dvw`, `%`, `fr`, `clamp()` etc.**: NOT flagged as raw dims. We only flag `px`, `rem`, `em` because those are the units that map directly to the narve token scale.
- **Off-scale font-sizes**: many `font-size` hits use values that are NOT on the published `--text-*` scale (e.g. `10.5px`, `12.5px`, `13.5px`, `15px`, `17px`). These are double violations — they bypass tokens AND they sit between published scale rungs. Files with the most off-scale font-sizes: `admin-shell.css` (19), `preview.css` (16), `prerelease.css` (16), `settings_api_keys.css` (15), `settings_api_key_reveal.css` (9).
- **`font-family` token compliance is high** — only 40 hits across 82 files, mostly in `poster.css` (7 — repeated `'Inter', sans-serif` declarations), `settings_embeds.css` (4), and `settings_webhooks.css` (3). A handful of files still use the literal Inter / Geist Mono / SFMono fallback chains instead of `var(--font-{ui,body,display,mono})`. None introduces a 4th typeface, so the three-typeface hard rule is intact.


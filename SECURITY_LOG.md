# Security log

> **Append-only.** Never edit past entries. One line per rotation, compromise
> investigation, or production security event.
>
> Format: `YYYY-MM-DD  action  secret_or_endpoint  (reason)  verified_how`

## 2026-04-23

- `2026-04-23`  **audit**  git history  (config hygiene pass)  no committed secrets found — only `.env.example` in history; no `sk_live_*`, `sk_test_*`, `whsec_*`, `sk-ant-*`, AWS, or Google key shapes detected
- `2026-04-23`  **tool**  added TruffleHog CI  (`.github/workflows/secret-scan.yml`)  `--only-verified --fail` on every push + PR; workflow will fail PRs that introduce a verifiable secret
- `2026-04-23`  **tool**  added `gateway/config.py::validate_config()`  enforces REQUIRED env vars at startup; `PRODUCTION=1` → `sys.exit(2)` on any violation

## Template

```
YYYY-MM-DD  rotated  STRIPE_SECRET_KEY  (annual)  verified via test webhook → 200 in logs
YYYY-MM-DD  compromised  SITE_ACCESS_TOKEN  (leaked in Discord screenshot)  rotated + re-distributed via 1Password; old token auto-rejected on first request
YYYY-MM-DD  audit     git history          (quarterly)  no hits
YYYY-MM-DD  incident  /api/predictions     (rate-limit bypass reported)  patched in commit abc1234; disclosed to reporter
```

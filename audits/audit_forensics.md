# Forensics Subsystem Audit

**Scope:** `gateway/forensics/` (signer + extract_watermark) plus every
forensics-adjacent surface that exposes, persists, or operates on its
state. The audit focuses on:

1. Trace-watermark integrity (HMAC keying, deterministic recovery).
2. Evidence-collection auth (who can upload a leaked screenshot/payload).
3. Admin-only access to the forensics tool.
4. Audit-log coverage of every forensic operation.

**Out of scope:** pre-release page (`gateway/static/prerelease.html`,
`gateway/static/pages/prerelease.css`) — per hard rule.

**Verification mode:** read-only static analysis. No code mutated.

## Files reviewed

| Path | Role |
|---|---|
| `gateway/forensics/__init__.py` | Namespace doc; no logic. |
| `gateway/forensics/signer.py` | Per-user JSON-payload signing (decimal precision, shuffle, sentinels). |
| `gateway/forensics/extract_watermark.py` | Offline + admin-route leak-recovery tool (OCR, sentinel, numeric paths). |
| `gateway/watermark.py` | Per-page visible SVG + invisible canvas seed generator. |
| `gateway/security_routes.py` | Registers `/admin/security/forensics{,/analyze}` + `/api/security/capture-attempt`. |
| `gateway/email_system/watermark.py` | Per-recipient HMAC email watermark + reverse lookup. |
| `gateway/admin_routes.py` (lines 825–1005) | `/admin/trace-watermark` reverse-lookup endpoint. |
| `gateway/security/audit.py` | Audit-log emitter and action catalog. |
| `gateway/migrations/070_watermark_seeds.py` | `watermark_seeds` table. |
| `gateway/migrations/071_forensic_sentinels.py` | `user_forensic_seeds` + `sentinel_predictions`. |
| `gateway/migrations/175_email_watermarks.py` | `email_watermarks` table. |
| `gateway/server.py:2359-2488, 5343-5371, 6162-6166, 2811-2816` | `_forensic_sign`, `_inject_watermark_layer`, `_require_admin_user`, `_require_super_admin`. |
| `gateway/tests/test_forensics.py` | Seed determinism + scoring discriminator. |
| `gateway/tests/test_email_watermark.py` | Email-watermark + trace-route negative tests. |
| `gateway/api_public/auth.py:170-182` | Public-API call site of `sign_response`. |

---

## 1. Trace-watermark integrity

### 1a. Data-level watermark (`signer.py`)
- Per-user 32-bit seed minted by `secrets.randbits(32)` and stored in
  `user_forensic_seeds(user_id PRIMARY KEY)` with a `rotation_version`
  counter for revocation (`rotate_seed`, lines 77–92).
- `get_or_create_seed` is the single mint path; failure falls back to
  `sha256("narve-wm-fallback:{user_id}")[:4]` (lines 70–74). Two
  observations: (i) the fallback is deterministic per `user_id` so a
  burned fallback can be predicted by anyone reading the source, and
  (ii) the fallback only fires when the DB write fails — in steady state
  every user has a true random seed.
- Sentinel ids derive from `sha256("narve-sentinel:{user_id}:{endpoint}:{seed}:{n}")[:16]`
  (line 174) and are persisted to `sentinel_predictions` with a 180-day
  TTL via `expires_at` (line 214). No background sweep deletes expired
  rows; `expires_at` is informational only.
- Recovery primitive (`score_payload_against_seed`) ignores rows whose
  signable key value is not an `(int, float)` (line 304), so a leaked
  payload with stringified numbers (`"0.42"` instead of `0.42`) silently
  scores 0/0 and triggers no match — caller (`_numeric_path`) returns
  `None` cleanly.
- `recover_seed_from_numeric_payload` confidence floor is 0.85
  (line 335) — comment claims "0.65" but the code uses 0.85; cosmetic
  doc drift, not exploitable.

### 1b. Visible / invisible page watermark (`watermark.py`)
- Per-(user_id, session_suffix) seed via `sha256("narve-wm:{user_id}:{session_id}")[:4]`
  (line 51). The session_id is a SHA-256 hex hash of the cookie value,
  so the seed is keyed against a hashed session — a stolen seed cannot
  be trivially mapped back to a cookie.
- SVG content is HTML-escaped before splicing (lines 89–105) — no XSS
  vector even if the email/IP contains `<`/`&`.
- `mask_ip` strips the last IPv4 octet / last two IPv6 groups before
  rendering into the SVG — defence against an admin doxxing themselves
  via screenshot. Good.
- `session_suffix` normalises non-hex inputs by hashing first, so a raw
  cookie never reaches the SVG output.

### 1c. Email watermark (`email_system/watermark.py`)
- HMAC-SHA256 keyed with `EMAIL_WATERMARK_KEY` (line 70). HMAC choice
  (not raw SHA-256) is correct — prevents length-extension forgery and
  prevents an outsider with a few leaked watermarks from mapping other
  user ids.
- If `EMAIL_WATERMARK_KEY` is unset, helpers return empty strings and
  `record_watermark` is a no-op (lines 80–82) — dev environments degrade
  gracefully; no fixed-fallback fingerprint that would un-discriminate
  recipients.
- `email_id(template, user_id, batch_ts)` buckets to day resolution
  (line 226) so retries within 24 h re-derive to the same watermark.
  Acceptable trade-off; documented in module docstring.
- Steganographic encoding uses U+200B / U+200C (lines 65–66) — avoids
  U+200D (ZWJ) so adjacent emoji shaping is unaffected.

### 1d. Findings — integrity
- **INFO-1:** The data-signer fallback (sha256 of a fixed prefix) is
  deterministic per user_id. If the `user_forensic_seeds` table is ever
  unreadable in production (corruption, disk-full), every signed
  payload uses a publicly-predictable seed. Mitigation: monitor
  `forensic seed fetch/create failed` warning log.
- **INFO-2:** `expires_at` on sentinels is never honoured. The
  `_sentinel_path` SELECT (extract_watermark.py:140-145) does
  `ORDER BY injected_at DESC LIMIT 5000` with no `WHERE expires_at >
  now()` clause. Old sentinels remain attributable forever; no live
  data leak, just dead-row clutter. Low priority.
- **INFO-3:** Doc-vs-code drift in `signer.recover_seed_from_numeric_payload`:
  docstring says floor 0.65, code uses 0.85. Code wins. Update
  docstring or constant — pick one.

---

## 2. Evidence-collection auth

The only evidence-upload surface is `POST /admin/security/forensics/analyze`
(`security_routes.py:325-384`). Handler chain:

1. `_require_super_admin(request)` — calls `_require_admin_user` then
   refuses unless `admin_level >= 2` (server.py:6162-6166).
2. CSRF: POST falls under the global `CSRFMiddleware` enforcement (no
   exemption path) — `_csrf` cookie + `x-csrf-token` header or
   `_csrf` form field required. `security/csrf.py:189-264`.
3. Admin-mutation rate limit: 30 mutations per 5 min keyed by admin
   email — `_require_admin_user` lines 5363-5369.
4. Global body-size cap: 1 MB via `SecurityHeadersMiddleware` (server.py:944-947).
5. Audit log written after analysis (security_routes.py:358-370).

`/api/security/capture-attempt` (lines 162-230) is the client-side
evidence channel. Authenticated-only (`current_user` check at line 173);
per-user rate-limit 60/min keyed `capture-attempt:{user_id}` (line 180);
metadata clipped to 4 KB / event_type to 64 bytes / user-agent to 256
bytes via `record_security_event` (line 80-86) — bounded write. Flood
alert at 6 events/10min single-fires an admin email (lines 219-228).

`/admin/trace-watermark` (admin_routes.py:839-954) is the email-watermark
reverse-lookup endpoint:
- `_require_admin_user(request)` — any admin level OK (line 855). Per
  comment line 833-834 this is deliberate: "incident response shouldn't
  require a super-admin escalation."
- Per-admin rate limit 10/hour keyed `trace-watermark:{admin_key}`
  (line 861).
- Query param `id` is regex-validated `^[0-9a-f]{4,12}$` (line 870).
- Out-of-band notification email to `EMAIL_FORENSIC` → `LEGAL_EMAIL` →
  hard-coded `legal@narve.ai` (lines 974-1005). Fire-and-forget so a
  stalled mailer doesn't block the response.

### 2a. Findings — auth
- **HIGH-1:** `extract_watermark._ocr_path` accepts whatever
  `image_bytes` we hand it and invokes `pytesseract.image_to_string`
  on an `Image.open(io.BytesIO(image_bytes))` (lines 66-72). The
  global 1 MB body cap bounds memory, but Pillow has had decompression-
  bomb CVEs and `PIL.Image.MAX_IMAGE_PIXELS` is not set anywhere in the
  forensics module. A super-admin uploading a malicious crafted image
  (or an attacker who has compromised a super-admin account) could
  pivot to RCE-class behaviour via the Pillow parser. Mitigations:
  (i) the route is super-admin-gated and (ii) 1 MB cap reduces blast
  radius. But add `Image.MAX_IMAGE_PIXELS = 25_000_000` (or similar)
  and wrap the OCR call in a `signal`-based timeout. The handler also
  never validates `screenshot.content_type` — an uploader can ship
  any binary blob as `image/png`.

- **MED-1:** `admin_forensics_analyze` swallows every exception during
  upload read (`except Exception: image_bytes = None`, line 339-340).
  A super-admin who uploads a corrupt file gets a silent OCR-skipped
  result with no error displayed — they will not know the OCR path
  was effectively a no-op. Surface a "screenshot could not be read"
  notice in the result panel.

- **MED-2:** The handler computes `text_blob or None` with no length
  cap (line 354). A 1 MB blob (allowed by the global cap) goes straight
  into `_sentinel_path` which then runs a Python `in` substring search
  against up to 5000 cached sentinel records (extract_watermark.py:151).
  Each row's `payload_json.title` is also subjected to `title.lower()
  in lower`. Worst-case O(5000 × 1 MB) — measurable CPU on the request
  thread. Cap `text_blob` at e.g. 64 KB before passing into
  `identify_leak`.

- **LOW-1:** `/admin/trace-watermark` (admin_routes.py:855) requires
  only `_require_admin_user`, not super-admin. The audit doc at
  line 833 explicitly justifies this. Compatible with policy but worth
  re-deciding: an email-watermark lookup deanonymises a Pro subscriber.
  If you want super-admin parity with the forensics tool, change line
  855 to `_require_super_admin`.

- **LOW-2:** `_resolve_by_session_suffix` (`extract_watermark.py:82-98`)
  builds a `LIKE '%{suffix}%'` query. `suffix` is regex-validated
  `[a-f0-9]{4,16}` via `_SID_RE` at the only call site (line 53), so
  it's safe from injection. Still: pin the validation to the call
  site so a future refactor can't bypass the regex.

---

## 3. Admin-only access

| Endpoint | Method | Guard |
|---|---|---|
| `GET  /admin/security/forensics` | GET | `_require_super_admin` (security_routes.py:308) |
| `POST /admin/security/forensics/analyze` | POST | `_require_super_admin` (line 333) + CSRF + admin mut-rate-limit |
| `GET  /admin/trace-watermark` | GET | `_require_admin_user` (admin_routes.py:855) + per-admin 10/h rate-limit |
| `POST /api/security/capture-attempt` | POST | authenticated user (line 173) + per-user 60/min rate-limit |
| `POST /settings/privacy/toggles` | POST | authenticated user (security_routes.py:268-270) |
| `GET  /admin/security/bulk-fetches` | GET | `_require_admin_user(page=True)` (line 281) |

Module-import side effects: `security_routes.py:55-64` runs an idempotent
`ALTER TABLE users ADD COLUMN ...` for two privacy-pref booleans. Done
inside try/`OperationalError pass` — re-runs are safe. No table created
here that bypasses migrations.

`_require_super_admin` (server.py:6162-6166) gates on
`user.get("admin_level", 0) < 2`. The fall-through `(0)` is a fail-closed
default — a session dict missing `admin_level` is treated as level 0.
Good.

### 3a. Findings — access
- **MED-3:** Both forensics admin endpoints register via the FastAPI
  `app.get(...)` / `app.post(...)` decorators **without**
  `include_in_schema=False` (security_routes.py:420-421). The OpenAPI
  schema therefore advertises the existence of `/admin/security/forensics`
  and `/admin/security/forensics/analyze` to anyone hitting `/openapi.json`.
  Compare admin_routes.py:3128-3131 which correctly hides the
  trace-watermark route with `include_in_schema=False`. Hide the
  forensics routes from the schema for parity and to avoid telegraphing
  the existence of a sentinel-injection scheme.

---

## 4. Audit-log coverage of every forensic operation

| Operation | Code path | Audit row? | Action constant |
|---|---|---|---|
| Forensic seed mint (auto, on first list-endpoint hit) | `signer.get_or_create_seed` | **no** | — (data-plane, not admin) |
| Forensic seed rotation | `signer.rotate_seed` | **no** | — (no caller — see §4a) |
| Sentinel injection | `signer._inject_sentinels` → `_record_sentinel` | **no** (only the DB row in `sentinel_predictions`) | — |
| List-endpoint signing | `server._forensic_sign`, `api_public.auth.sign_if_available` | **no** | data-plane |
| `POST /admin/security/forensics/analyze` | `security_routes.admin_forensics_analyze` | **yes** | `"forensics.analyze"` (free-form string, see §4b) |
| `GET /admin/trace-watermark` | `admin_routes.trace_watermark_route` | **yes** | `EMAIL_WATERMARK_TRACE` |
| `POST /api/security/capture-attempt` | `security_routes.capture_attempt` | **yes** (different table — `security_events`) | n/a |

### 4a. Findings — coverage
- **HIGH-2:** `signer.rotate_seed` is reachable from nowhere in the
  codebase. `grep -rn "rotate_seed" gateway/ --include='*.py'` returns
  one hit (the test file `test_forensics.py:55`). The module docstring
  advertises rotation as "admin-driven rotation — re-mints the seed and
  bumps rotation_version" (signer.py:77-78) but no admin route, no CLI,
  no management command invokes it. When a seed is burned (sentinel
  pattern recognised, scheme reverse-engineered), there is no way to
  rotate without an SSH+`python -c` session against the live DB. Either
  (a) wire `rotate_seed` to a super-admin POST that audit-logs the
  rotation, or (b) remove the helper and document the manual procedure.

- **HIGH-3:** `"forensics.analyze"` is passed as a free-form string at
  security_routes.py:363, **not** a member of `security.audit.AuditAction`.
  Compare with `EMAIL_WATERMARK_TRACE` (audit.py:69) which has a
  catalog entry, an `ACTION_LABELS` mapping (line 134), and a fallback
  symbol resolution in admin_routes.py:883-884
  (`getattr(_a.AuditAction, "EMAIL_WATERMARK_TRACE", None) or ...`).
  Three knock-on effects: (i) audit-log filter dropdown (`audit.py:99-102`
  `ALL_ACTIONS = tuple(...)` built from AuditAction class attrs) will
  not list `forensics.analyze` so the admin search/filter page misses
  it; (ii) `ACTION_LABELS` lookup yields raw string; (iii) any future
  rename or typo at the call site silently writes the wrong action and
  no test catches it because there's no constant. Add
  `FORENSICS_ANALYZE = "forensics.analyze"` to `AuditAction` and an
  entry to `ACTION_LABELS`.

- **MED-4:** `admin_forensics_analyze` writes the audit row **after**
  running `extract_watermark.identify_leak` (security_routes.py:351-369).
  If `identify_leak` raises (e.g. Pillow decompression bomb, DB outage),
  the audit row is never written — an admin can attempt forensics with
  no trail. Pattern in admin_routes._audit is to log first, mutate
  after; mirror it here. At minimum, move the `_audit.log_action` into
  a `try/finally` so an attempt is logged even when analysis fails.

- **MED-5:** `_audit.log_action` swallows exceptions (security/audit.py:267-268
  `except Exception as e: log.warning(...)`). On a DB outage every
  forensic action will silently fail to audit and only emit a warning
  log line. Compatible with the documented "NEVER raises" contract but
  forensic actions in particular should be loud — consider routing
  audit failures for `forensics.analyze` / `email.watermark_trace` to
  Sentry-error level (not warning) so PagerDuty/incidents wake up.

- **LOW-3:** Data-plane signing (`_forensic_sign`,
  `sign_if_available`) writes no log line on failure beyond
  `log.warning("forensic sign failed endpoint=%s: ...")`. A complete
  signer outage (every API response silently returned unsigned) would
  destroy attribution capacity without raising. Add a counter / Sentry
  breadcrumb when this path warns more than N/min.

- **LOW-4:** Sentinel injection persistence
  (`signer._record_sentinel`, lines 200-218) swallows failures the same
  way. A successful injection that fails to record means the sentinel
  reaches the user but is not in the DB to match against later — a silent
  attribution miss. Surface as a counter.

---

## 5. Severity tally

| Severity | Count | Items |
|---|---|---|
| HIGH | 3 | HIGH-1, HIGH-2, HIGH-3 |
| MED  | 5 | MED-1, MED-2, MED-3, MED-4, MED-5 |
| LOW  | 4 | LOW-1, LOW-2, LOW-3, LOW-4 |
| INFO | 3 | INFO-1, INFO-2, INFO-3 |

## 6. Top 3 fixes (by attacker-leverage × ease-of-fix)

1. **HIGH-3** — Add `AuditAction.FORENSICS_ANALYZE = "forensics.analyze"`
   to `gateway/security/audit.py` (plus `ACTION_LABELS` entry). Without
   it the audit search/filter never lists forensic tool use; today the
   admin team has no clean way to enumerate "every time the forensics
   tool ran". One-line constant + one-line label, no behavioural change
   to the write path. Highest-value lowest-risk fix.

2. **HIGH-2** — Either wire `signer.rotate_seed` to a super-admin
   `POST /admin/security/forensics/rotate/{user_id}` that audit-logs
   `forensics.rotate_seed`, or delete the helper. Today the only way to
   burn a leaked seed is a live-DB `UPDATE` — that's an unauditable
   privileged write and will tempt operators to fix things off-the-books.

3. **HIGH-1 / MED-3 (combined)** — Harden the OCR upload path:
   (a) `PIL.Image.MAX_IMAGE_PIXELS = 25_000_000` at module import in
   `extract_watermark.py`; (b) validate `screenshot.content_type` against
   an `image/{png,jpeg,webp}` allowlist; (c) cap `text_blob` to 64 KB
   in `admin_forensics_analyze` before forwarding; (d) add
   `include_in_schema=False` to the two forensics route registrations
   (security_routes.py:420-421) so they stop appearing in the public
   OpenAPI schema. All four are mechanical changes inside files already
   touched by this audit; collectively they eliminate the only realistic
   RCE-class pivot in the forensics surface.

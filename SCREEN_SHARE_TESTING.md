# narve.ai — screen-share & forensic-watermark manual test plan

The automated suite (`gateway/tests/test_watermark.py`) covers the signer,
the recovery tool, the bulk-fetch counter, and the privacy-preference
round trip. The things it **can't** cover — actual pixels on a real
screen, browser-level capture APIs, devtools timing heuristics — live in
this checklist.

Run through this end-to-end before any release that touches
`watermark.py`, `forensics/`, `security_routes.py`, `watermark.js`, or
`middleware/bulk_data_ratelimit.py`.

> **What the product actually delivers:** realistic deterrence + perfect
> forensic traceback. We do **not** and cannot "block screen recording"
> on a web app. Do not promise that anywhere in the UI. The copy should
> always be "all sessions are watermarked for leak attribution."

---

## Preconditions

- A production-like build running locally (`PRODUCTION=0` is fine — the
  watermark inject path fires as long as you're logged in).
- Two test accounts (e.g. `you@test.com` super-admin, `b@test.com` plain
  user) so the forensics tool has at least two seed rows to distinguish
  between.
- A second browser profile or incognito window, for generating the
  "leaker" session cleanly separate from the "admin" session you'll use
  to investigate.

Log in as the test user on the leaker profile. In a second tab on the
admin profile, log in as the super-admin.

---

## 1. Visible forensic watermark overlay

**Steps**

1. On the leaker profile, open `/dashboards` and `/admin`.
2. Open DevTools → Elements → search for `nv-watermark-visible` and
   `nv-watermark-canvas`. Both should be direct children of `<body>`.
3. Confirm the `<div id="nv-watermark-visible">` carries an inline
   `background-image: url("data:image/svg+xml;base64,…")`.
4. Macro-zoom the page to 200% (Cmd/Ctrl +) and squint at any blank area.
   The tiled pattern is visible but not intrusive.
5. Take a native screenshot (Cmd+Shift+4 on mac, PrintScreen on windows).
6. Open the screenshot in an image viewer at 100% zoom. Confirm:
   - Your email is legible somewhere in the tile.
   - A `uid:<N>` token is legible.
   - A `sid:<8-hex-chars>` token is legible.
   - A masked IP like `81.147.*.x` is legible.
7. Crop out the visible watermark in an image editor. Save the cropped
   copy to `/tmp/leaked-cropped.png`.

**Pass criteria**

- All four forensic lines are legible without zoom.
- The watermark does **not** sit on top of any clickable control (test
  by clicking through it on a button behind a tile).
- Light-theme pages stay readable (the CSS drops opacity to 0.045 +
  `mix-blend-mode: multiply`).

**Failure modes to flag**

- Watermark blocking clicks → pointer-events regression, check
  `static/watermark.css`.
- Missing `sid:` or `uid:` fragment → `current_user()` is returning a
  row shape the injector doesn't handle, check
  `server._inject_watermark_layer`.

---

## 2. Steganographic canvas survives cropping

**Steps**

1. With the cropped screenshot from §1 step 7, run the recovery tool:

   ```bash
   cd ~/Habbig/gateway
   python3 -m forensics.extract_watermark --image /tmp/leaked-cropped.png
   ```

2. If pytesseract is installed the OCR path may still catch a residual
   `uid:` fragment; otherwise it falls back to the sentinel / numeric
   paths.

**Pass criteria**

- The tool returns a JSON result with `user_id` matching the leaker
  profile. Confidence ≥ 0.85 is expected when OCR works.

**If pytesseract is not installed** the `--image` path returns "no
match" — that's fine, the automated tests already cover the numeric and
sentinel recovery paths.

---

## 3. Data-level forensic watermark

**Steps**

1. While logged in as the leaker, open a new tab to
   `https://<host>/api/markets/unified?limit=60` (or any list endpoint
   you wired the signer into).
2. Save the JSON response to `/tmp/leaked.json`.
3. Log out and back in as the admin.
4. Load `/admin/security/forensics`.
5. Paste the leaked JSON into the "Numeric payload" textarea (paste the
   `markets` array, not the whole wrapper dict).
6. Click **Analyse**.

**Pass criteria**

- The result card renders `Highest-likelihood source: user_id=<leaker>`.
- Confidence ≥ 0.85.
- An audit log entry is written to `audit_log` / `security.log` for the
  `forensics.analyze` action.

---

## 4. Sentinel-row recovery

**Steps**

1. Have the leaker hit a list endpoint that returns ≥50 items (e.g.
   `/api/markets/unified?limit=100`).
2. Pick one of the returned rows whose `id` starts with `s_…` — that's
   a sentinel.
3. Paste the sentinel `id` (without the `s_` prefix) into the "Leaked
   text" field on `/admin/security/forensics`.
4. Click **Analyse**.

**Pass criteria**

- `user_id = <leaker>`, source = `sentinel`, confidence ≥ 0.9.

---

## 5. Capture-attempt detection

**Steps**

1. Leaker profile: open `/dashboards`.
2. Press `PrintScreen` (windows) or `Cmd+Shift+4` (mac).
3. Select ≥ 500 chars of dashboard text and press `Cmd/Ctrl+C`.
4. Open DevTools → Network. Paste the below into the Console:

   ```javascript
   navigator.mediaDevices.getDisplayMedia({video: true}).catch(() => {});
   ```

5. Load `/admin/security/bulk-fetches` on the admin profile (or just
   tail `security.log` on the server).

**Pass criteria**

- Each of the three events (shortcut, bulk_copy, getDisplayMedia) shows
  up as its own row in `security_events` with the expected `event_type`.
- The toast `narve.ai — all sessions are watermarked for leak
  attribution.` flashes after the `getDisplayMedia` call.
- At the 6th event within 10 minutes, `security.log` carries a line at
  ERROR level: `capture_attempt FLOOD user_id=…`.

---

## 6. Page-visibility blur

**Steps**

1. Leaker profile on `/dashboards`, focus on the page.
2. Switch to another window / tab for 4 seconds, then switch back.

**Pass criteria**

- After ~3 s the dashboard goes blurry (`body.nv-privacy-blur` class,
  `filter: blur(14px)`).
- On refocus the blur clears inside 200 ms.
- Flipping `/settings/privacy` → "Auto-blur when window loses focus" OFF
  and reloading suppresses the behaviour.

---

## 7. Devtools detection → blur

**Steps**

1. Leaker profile on `/dashboards` (MUST be `/dashboard`, `/admin`,
   `/predictions`, `/markets`, or `/sources` — other paths skip the
   heuristic by design).
2. Open DevTools.

**Pass criteria**

- Inside a second or two the page blurs. A `devtools_opened` event lands
  in `security_events`.
- Close DevTools → blur clears.
- If the user disables breakpoints (Sources panel → checkbox), the
  heuristic will stop firing. **That's expected**, document it as a
  deterrent not a defence.

---

## 8. Bulk-fetch rate limit

**Steps**

1. Leaker profile: open a terminal and run (substituting a real session
   cookie):

   ```bash
   for i in $(seq 1 6); do
     curl -s -b "narve_session=…" "https://<host>/api/markets/unified?limit=100&page=$i" \
       | jq '.markets | length'
   done
   ```

2. After the 50th page (or equivalent total of 5000+ rows), the next
   request should return HTTP 429.

**Pass criteria**

- 429 response body includes `"error": "Hourly data budget exceeded."`,
  `"budget": 5000`, and a `Retry-After` header.
- `/admin/security/bulk-fetches` shows the leaker at the top of the
  list with a FLAGGED badge once past 20 000 rows/24 h.

---

## 9. Admin forensics toolkit

Already exercised by §3 and §4. Additional spot-check:

- Super-admin only: attempt to GET `/admin/security/forensics` as a
  regular admin (level 1). Expect 403.
- Any use by a super-admin appears in `audit_log` with
  `action = forensics.analyze`.

---

## Regression smoke (< 60 s)

On a stock install:

```bash
cd ~/Habbig/gateway
python3 -m pytest tests/test_watermark.py -q
```

16 tests, should all pass. If any fail, do not deploy.

---

## Known deliberate gaps

- The visible watermark **does not** survive a high-quality re-photograph
  of the screen from a neighbouring monitor if the photographer masks it
  in post-production. That's why we ship the canvas + data-level
  watermarks in parallel.
- External screen-capture tools (OBS, Camtasia, native OS screen
  recording) are invisible to our JS. Watermarks (both visible and
  canvas) are the defence there.
- The devtools-timing heuristic fails against any user who toggles
  "Disable breakpoints" in Sources. Correct — this is a deterrent, not
  a gate.

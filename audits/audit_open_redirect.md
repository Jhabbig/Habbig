# Open Redirect Audit — gateway/

**Date:** 2026-05-15
**Scope:** Every `RedirectResponse(url=...)` and `Response(status_code=302, headers={"Location": ...})` in `/Users/shocakarel/Habbig/gateway/` (excluding `tests/`).
**Goal:** For each call site whose destination URL is built from a query parameter, form field, or cookie, trace the URL source and verify an allowlist check is in place (e.g. `if not url.startswith("/"): abort`).

---

## Headline numbers

| Metric | Count |
|---|---|
| Total `RedirectResponse(...)` call sites in `gateway/` (excl. tests) | **153** |
| Raw `Response(status_code=302, headers={"Location": ...})` call sites | **0** (no raw Location-header redirects anywhere) |
| Sites whose destination is a **string literal or f-string with `/` prefix** | 145 |
| Sites whose destination uses a **variable / function return** | **8** |
| Sites whose variable is sourced from a **trusted allowlist or server-only state** | 5 |
| Sites that **embed a user-controlled value into the URL host without sanitisation** | **3** (all in one handler — `POST /subproduct-signup`) |
| **Distinct exploitable redirect vulnerabilities** | **2** (line 202–205 and 208–211; line 219–222 is reached only after a successful allowlist check) |

The codebase contains **no** classic `?next=`, `?return_to=`, or `?redirect_uri=` query-parameter consumer that drives a redirect destination. A grep for
`query_params.*(next|redirect|return)|form\.get.*(next|redirect|return)` returns zero non-test matches. The `nxt = request.url.path` value used in
`profile_routes.py:185-187` is *embedded* into a `/login?next={nxt}` URL but is never read back by any handler to decide where to redirect, so it is not an open-redirect vector.

---

## Methodology

1. Enumerated every `RedirectResponse(...)` (incl. multi-line) under `gateway/` excluding `tests/`.
2. Filtered out string-literal destinations (`"/foo"`, `f"/foo/{int_id}"`) — those are same-origin by construction.
3. For every site whose destination is a variable, function return, or f-string with a non-`/` prefix, traced the variable's data flow back to its source (query param, form field, cookie, `request.state`, DB row, allowlist, env var, etc.).
4. For each user-controllable source, looked for an allowlist check before the redirect: regex/dict membership/`startswith("/")`/`urlparse(...).netloc` check.
5. Also searched for `Response(status_code=302, ...)` and any code that sets a `Location` header manually — none found.

---

## Per-site triage (only sites with non-literal destinations)

### 1. `subproduct_signup_routes.py:202-205`  — **VULNERABLE (Open Redirect)**

```python
@app.post("/subproduct-signup")
async def subproduct_signup(
    request: Request,
    email: str = Form(""),
    subproduct: str = Form(""),
):
    attached = getattr(request.state, "subproduct", None)
    slug = (attached or subproduct or "").strip()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return RedirectResponse(
            f"https://{slug}.narve.ai/?error=email" if slug else "/",
            status_code=302,
        )
```

**URL source:** the `subproduct` form field. When the request hits the apex host (`narve.ai`), `SubproductMiddleware` sets `request.state.subproduct = None`, so `slug` falls through to the *raw, unvalidated form value*.

**Allowlist check:** **none** at this point in the flow. `_stripe_price_id(slug)` would consult `SUBPRODUCTS` (an allowlist), but it is called on line 206 — *after* this redirect.

**Exploit:** POST to `https://narve.ai/subproduct-signup` with form fields `subproduct=evil.com#` and `email=` (empty). Server responds:

```
HTTP/1.1 302 Found
Location: https://evil.com#.narve.ai/?error=email
```

Browser parses everything after `#` as fragment → navigates to `https://evil.com`. Verified with `starlette.responses.RedirectResponse` — `#` and `?` are not URL-encoded in the `Location` header.

Equivalent payloads:
- `subproduct=evil.com?` → `Location: https://evil.com?.narve.ai/?error=email` (browser navigates to `evil.com` with the rest as query string)
- `subproduct=evil.com\` → `Location: https://evil.com%5C.narve.ai/?error=email` (some browsers normalise the `\`, leading to ambiguous host parsing — defence in depth concern)

**Severity:** Medium. Requires the attacker to lure a victim into POSTing the form (CSRF token, if present, has to be valid) or to construct a phishing page that auto-submits a form to `narve.ai/subproduct-signup`. The handler accepts plain `<form method="post">` per its docstring ("button can be a plain `<form method=post>` with no JS"), and the path is reachable without auth.

**Fix sketch:**
```python
from subproduct import SUBPRODUCTS  # the catalogue (already imported elsewhere)
if slug and slug not in SUBPRODUCTS:
    slug = ""    # collapse unknown slugs to the apex fallback
```

---

### 2. `subproduct_signup_routes.py:208-211`  — **VULNERABLE (Open Redirect)**

```python
price_id = _stripe_price_id(slug)
if not price_id:
    return RedirectResponse(
        f"https://{slug}.narve.ai/?error=config" if slug else "/",
        status_code=302,
    )
```

**URL source:** same `slug` as #1.

**Allowlist check:** `_stripe_price_id(slug)` returns `None` for any slug not in `SUBPRODUCTS`, but the failure branch *still embeds the unvalidated slug into the redirect host*. Same exploit shape as #1, triggered by passing a valid email + an unknown slug.

**Same fix:** validate `slug ∈ SUBPRODUCTS` before using it in the redirect string.

---

### 3. `subproduct_signup_routes.py:219-222`  — SAFE

```python
try:
    user_id = _create_or_get_shell_user(email)
    url = await _build_checkout_session(...)
except Exception:
    return RedirectResponse(
        f"https://{slug}.narve.ai/?error=checkout",
        status_code=302,
    )
```

This redirect is reached **only after `_stripe_price_id(slug)` returned non-`None`** (line 206), which guarantees `slug ∈ SUBPRODUCTS`. The slug is therefore allow-listed, so the f-string host is one of the known subproduct subdomains. **Safe.**

---

### 4. `subproduct_signup_routes.py:223`  — SAFE

```python
return RedirectResponse(url, status_code=302)
```

**URL source:** `url = await _build_checkout_session(...)` which returns `str(session.url)` from the Stripe SDK (`stripe.checkout.Session.create`). Trusted vendor URL (`https://checkout.stripe.com/...`). **Safe.**

---

### 5. `server.py:1490`, `7883`, `7890`, `7911`  — SAFE

```python
return RedirectResponse(f"https://{apex}/gate", status_code=302)
return RedirectResponse(f"https://{apex}/", status_code=302)
return RedirectResponse(f"https://{apex}/gate", status_code=302)
return RedirectResponse(f"https://{apex}/billing?dashboard={key}", status_code=302)
```

**URL source:** `apex = _request_apex(request)`. Defined at `server.py:86-100`:

```python
def _request_apex(request: Request) -> Optional[str]:
    host = _request_host(request)
    if not host:
        return None
    for apex in ALLOWED_DOMAINS:
        if host == apex or host.endswith("." + apex):
            return apex
    return None
```

`ALLOWED_DOMAINS` is built from `config.json` (`DOMAIN` + `domain_aliases`) — a server-side allowlist, never user input. The function returns a value from the allowlist or `None`; the caller handles `None` by falling back to `DOMAIN` (the canonical apex). **`apex` is allowlisted by construction.**

`key` (at line 7911) is `SUBDOMAIN_TO_KEY.get(get_subdomain(request))` — only set if the subdomain is in a server-side `DASHBOARDS` config dict. **Allowlisted. Safe.**

---

### 6. `admin_routes.py:185`  — SAFE

```python
target_path = "/admin/impersonations" if admin and admin.get("is_admin") else "/login"
response = RedirectResponse(target_path, status_code=302)
```

`target_path` is a ternary over two string literals. **Safe.**

---

### 7. `admin_routes.py:483`  — SAFE

```python
redirect = f"/admin/flags/{key}"
if subproduct_key:
    redirect += f"?subproduct={subproduct_key}"
return RedirectResponse(redirect, status_code=302)
```

`key` is validated by `re.fullmatch(r"[a-z0-9_\-]{1,80}", key)` four lines earlier (line 453). `subproduct_key = _normalize_subproduct(form.get("subproduct"))` — `_normalize_subproduct` collapses unknown subproducts to `None`. Both components are constrained, and the URL is rooted at `/admin/flags/...` (same-origin). **Safe.**

---

### 8. `feedback_routes.py:611`  — SAFE

```python
target = f"/feedback/{new_id}" if public_flag else "/feedback?saved=private"
return RedirectResponse(target, status_code=302)
```

`new_id = int(cur.lastrowid or 0)` (line 589) — an integer. **Safe.**

---

### 9. `saved_views_routes.py:339`  — SAFE

```python
target = _scope_url_for(row["scope"], row["filters"], view_id=view_id)
response = RedirectResponse(url=target, status_code=302)
```

`_scope_url_for` (line 89) starts with `base = _SCOPE_URL.get(scope, "/dashboards")` — a hardcoded dict lookup with a same-origin path fallback. Filters are passed through `schema.filters_to_query` and `urlencode`. **Safe.**

---

### Profile-routes `_redirect_to_login` (line 185-187)  — SAFE but worth noting

```python
def _redirect_to_login(request: Request) -> RedirectResponse:
    nxt = request.url.path
    return RedirectResponse(f"/login?next={nxt}", status_code=302)
```

`nxt = request.url.path` is always the server-decoded path starting with `/`. The whole redirect URL is same-origin (`/login?next=...`). No code in the codebase reads the `next` query parameter at `/login` to drive a follow-on redirect — verified with grep:
```
grep -rn "\.get(['\"]next['\"])\|\['next'\]" --include="*.py" gateway/
```
returns only DB row indexing and link rendering — never a `RedirectResponse(next_value)` style consumer. **Safe.**

---

## Other patterns confirmed absent

| Pattern | Result |
|---|---|
| `Response(headers={"Location": ...}, status_code=302)` (raw Location header) | 0 hits |
| `request.headers.get("referer")` driving a redirect | 0 hits |
| Cookie value driving a redirect destination | 0 hits |
| `query_params.get("next" / "redirect" / "return_to" / "redirect_uri")` driving a redirect | 0 hits |
| Stripe `return_url` / `success_url` / `cancel_url` taken from user input | 0 hits — all hardcoded (`https://narve.ai/...`) or built from validated `slug` + env `APP_URL` |

---

## Top 3 findings (most exploitable / highest risk)

1. **`subproduct_signup_routes.py:202-205`** — Open redirect via `subproduct` form field on `POST /subproduct-signup` when email is invalid. F-string host `https://{slug}.narve.ai/...` accepts `#`/`?` injection. **Fix:** validate `slug ∈ SUBPRODUCTS` *before* the early-return redirects (move the dict check above the email check, or sanitise on entry).
2. **`subproduct_signup_routes.py:208-211`** — Same vulnerability as #1, on a different branch (slug not in `SUBPRODUCTS`). Same fix.
3. **(non-vuln, observation)** — `profile_routes.py:185-187` puts `request.url.path` into `?next={nxt}` but the parameter is never consumed. If a future PR adds a `/login` handler that reads `next` and redirects to it, this becomes the new top finding. Recommend either (a) `urllib.parse.quote(nxt, safe="/")` when embedding, and (b) any future consumer must verify `nxt.startswith("/") and not nxt.startswith("//")`.

---

## Summary

| | |
|---|---|
| Redirects audited | **153** |
| Vulnerable redirects | **2** |
| Vulnerable file | `gateway/subproduct_signup_routes.py` (lines 202-205 and 208-211) |
| All other redirect destinations | Same-origin string literal, validated slug, allowlisted apex, or vendor URL (Stripe) |

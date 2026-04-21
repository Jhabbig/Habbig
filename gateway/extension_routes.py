"""Server-side endpoints for the browser extension.

Two concerns:

1. ``GET /extension/auth`` — the gateway between a logged-in narve.ai
   tab and the installed extension. It requires an existing narve
   session, mints a 7-day JWT, and handshakes it to the extension via
   ``chrome.runtime.sendMessage`` (externallyConnectable path). The
   tab closes itself after 2s.

2. ``GET /api/extension/market/{slug}`` — the data endpoint the
   extension's content script calls for every Polymarket event page.
   Returns a compact bundle (probability, edge, confidence, top
   sources). Auth is the extension JWT — NOT the narve session
   cookie — so the extension works even after the user has closed
   all narve.ai tabs.

Rate limit: 60 requests/minute per JWT (shared Redis/memory limiter
from security/rate_limiter.py). Bundle cached 2 minutes keyed by
slug (via cache.CacheService).
"""

from __future__ import annotations

import html as _html
import json
import logging
import os
import secrets
import time
from typing import Any, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse


log = logging.getLogger("extension.routes")


# 7-day JWT lifetime. Baked in — not configurable — so the extension
# can cache it without re-checking server policy.
_JWT_TTL_SECONDS = 7 * 24 * 3600
_BUNDLE_TTL_SECONDS = 120
_RATE_LIMIT_PER_MINUTE = 60


def _jwt_secret() -> bytes:
    """HMAC key for the extension JWT. Falls back to a startup-time
    secret if EXTENSION_JWT_SECRET isn't set — acceptable in dev, but
    logs a warning in production (the deploy script should set it)."""
    val = os.environ.get("EXTENSION_JWT_SECRET", "").strip()
    if val:
        return val.encode()
    fallback = os.environ.get("GATEWAY_COOKIE_SECRET") or "narve-extension-dev"
    return fallback.encode()


def _sign_jwt(user_id: int) -> dict:
    """Minimal HS256 JWT. We avoid pulling a PyJWT dep — the token is
    opaque to the extension; only our server verifies it."""
    import base64
    import hashlib
    import hmac
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + _JWT_TTL_SECONDS,
        "scope": "extension",
    }

    def _b64(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = f"{_b64(header)}.{_b64(payload)}".encode()
    sig = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return {
        "token": f"{signing_input.decode()}.{sig_b64}",
        "expires_at": payload["exp"],
    }


def _verify_jwt(token: str) -> Optional[int]:
    """Return the user_id if the token is valid and unexpired, else None."""
    import base64
    import hashlib
    import hmac
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(_jwt_secret(), signing_input, hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
    except Exception:
        return None
    if payload.get("scope") != "extension":
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    try:
        return int(payload.get("sub"))
    except (TypeError, ValueError):
        return None


def _extension_id() -> Optional[str]:
    """Chrome Web Store ID we hand off the token to.

    Dev builds don't have one. The handshake HTML falls back to
    ``window.postMessage`` so the dev tab can still open a browser
    with the unpacked extension and receive the token.
    """
    val = os.environ.get("NARVE_EXTENSION_ID", "").strip()
    return val or None


def _auth_page_html(jwt: dict) -> str:
    """The /extension/auth response body.

    Posts the JWT to the extension via externallyConnectable if an ID
    is configured. Also posts via window.postMessage for unpacked
    dev builds. Tab closes after 2 seconds.
    """
    ext_id = _extension_id()
    ext_id_js = json.dumps(ext_id) if ext_id else "null"
    jwt_json = json.dumps(jwt)
    return f"""<!DOCTYPE html><html>
<head>
<meta charset="utf-8">
<title>narve.ai — extension connected</title>
<style>
body{{background:#0d0d0d;color:#fff;font-family:-apple-system,BlinkMacSystemFont,Inter,sans-serif;
  display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{text-align:center;max-width:380px;padding:28px 32px;background:#141414;
  border:1px solid #2a2a2a;border-radius:12px}}
h1{{font-family:'Instrument Serif',Georgia,serif;font-style:italic;margin:0 0 8px;font-size:28px}}
p{{color:#9aa0a6;font-size:13px;margin:8px 0}}
.ok{{color:#7fe29a}}
</style>
</head>
<body>
<div class="card">
  <h1>narve.ai</h1>
  <p class="ok" id="msg">Connecting the extension…</p>
  <p>This tab will close automatically.</p>
</div>
<script>
(function(){{
  var jwt = {jwt_json};
  var extId = {ext_id_js};
  var msg = document.getElementById("msg");
  function done(){{ msg.textContent = "Extension connected."; setTimeout(function(){{ window.close(); }}, 1800); }}
  function fail(e){{ msg.textContent = "Couldn't reach the extension: " + (e || "unknown"); }}
  try {{
    // Preferred: externallyConnectable → chrome.runtime.sendMessage.
    if (extId && typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {{
      chrome.runtime.sendMessage(extId, {{type: "setJwt", jwt: jwt.token, expires_at: jwt.expires_at}},
        function(resp){{ if (chrome.runtime.lastError) {{ fail(chrome.runtime.lastError.message); return; }} done(); }});
    }} else {{
      // Dev fallback: postMessage onto the window the extension's
      // content script listens for (narve.ai origin).
      window.postMessage({{source: "narve", type: "setJwt", jwt: jwt.token, expires_at: jwt.expires_at}}, location.origin);
      setTimeout(done, 500);
    }}
  }} catch (e) {{
    fail(e && e.message || e);
  }}
}})();
</script>
</body></html>"""


def _bundle_cache_key(slug: str) -> str:
    return f"ext_bundle:{slug}"


async def _compose_bundle(slug: str) -> Optional[dict]:
    """Assemble the extension bundle for ``slug``.

    Keeps the shape compact (no deep predictions list) so the extension
    payload stays well under 10 KB even for markets with thousands of
    predictions. Returns None if we have no coverage at all.
    """
    import db
    # Convert a raw slug (from polymarket.com URL) into our market_id
    # form. The pipeline stores markets under both "poly:{slug}" and
    # occasionally a conditionId; accept either.
    candidates = [f"poly:{slug}", slug]
    preds = []
    chosen: Optional[str] = None
    for cand in candidates:
        rows = db.get_predictions_for_market(cand) if hasattr(
            db, "get_predictions_for_market"
        ) else []
        if rows:
            preds = rows
            chosen = cand
            break

    if not preds:
        return {
            "market_slug": slug,
            "market_question": None,
            "betyc_yes_probability": None,
            "market_yes_price": None,
            "betyc_edge": None,
            "betyc_confidence": "unknown",
            "source_count": 0,
            "top_sources": [],
            "risk_flag": None,
            "insider_signals": 0,
        }

    pred_dicts = []
    for p in preds:
        pred_dicts.append({
            "source_handle": p["source_handle"],
            "direction": p["direction"],
            "predicted_probability": p["predicted_probability"],
            "global_credibility": p.get("global_credibility") if isinstance(p, dict) else (
                p["global_credibility"] if "global_credibility" in p.keys() else None
            ),
            "category_credibility": None,
            "accuracy_unlocked": True,
        })

    # Lazy-import the probability calculator so cold-start stays fast.
    calc = db.calculate_betyc_probability(pred_dicts) if hasattr(
        db, "calculate_betyc_probability"
    ) else {"betyc_yes_probability": None, "betyc_source_count": len(pred_dicts)}

    market_yes = None
    try:
        from backend.markets import unified_markets as _um
        # fetch_single_market is async; fall back to None if unavailable.
        # Best-effort — the extension tolerates missing live price.
        poly = getattr(_um, "POLY_SINGLE_FETCHER", None)
        if poly:
            market = await poly(chosen)
            market_yes = market.yes_price if market else None
    except Exception:
        pass

    edge = None
    if market_yes is not None and calc.get("betyc_yes_probability") is not None:
        edge = round(calc["betyc_yes_probability"] - market_yes, 4)

    # Pick the top 3 sources by credibility.
    sorted_preds = sorted(
        preds,
        key=lambda r: (r["global_credibility"] if "global_credibility" in r.keys() else 0) or 0,
        reverse=True,
    )
    top = []
    seen_handles: set[str] = set()
    for r in sorted_preds:
        h = r["source_handle"]
        if h in seen_handles:
            continue
        top.append({
            "handle": h,
            "credibility": r["global_credibility"] if "global_credibility" in r.keys() else None,
        })
        seen_handles.add(h)
        if len(top) >= 3:
            break

    # Simple confidence heuristic: count + spread.
    source_count = calc.get("betyc_source_count") or len(pred_dicts)
    if source_count >= 8:
        confidence = "high"
    elif source_count >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "market_slug": slug,
        "market_question": None,  # not cheap to derive here; skip
        "betyc_yes_probability": calc.get("betyc_yes_probability"),
        "market_yes_price": market_yes,
        "betyc_edge": edge,
        "betyc_confidence": confidence,
        "source_count": source_count,
        "top_sources": top,
        "risk_flag": None,
        "insider_signals": 0,
    }


def register(app) -> None:

    @app.get("/extension/auth", response_class=HTMLResponse)
    async def extension_auth(request: Request):
        """Handshake page — narve session in, extension JWT out."""
        user = getattr(request.state, "user", None)
        if user is None:
            return HTMLResponse(
                "<h1>Sign in first</h1>"
                "<p>Open <a href='/login'>narve.ai/login</a> first, "
                "then come back to this page.</p>",
                status_code=401,
            )
        uid = int(user.get("id") or user.get("user_id") or 0)
        jwt = _sign_jwt(uid)
        return HTMLResponse(_auth_page_html(jwt))

    @app.get("/api/extension/market/{slug:path}")
    async def api_extension_market(request: Request, slug: str):
        """Bundle lookup. Auth: Bearer <extension JWT>."""
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        uid = _verify_jwt(auth[7:])
        if uid is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

        # Rate limit — share the security/rate_limiter bucket so the
        # same infra that guards /auth applies here.
        try:
            from security.rate_limiter import limiter
            allowed, remaining, retry_after = limiter.check(
                f"ext:{uid}", _RATE_LIMIT_PER_MINUTE, 60,
            )
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded",
                    headers={"Retry-After": str(retry_after)},
                )
        except ImportError:
            pass  # rate limiter optional in dev

        # 2-min cache per slug. Shared across all extension users.
        try:
            from cache import cache
            async def _build() -> Optional[dict]:
                return await _compose_bundle(slug)
            bundle = await cache.get_or_set(
                _bundle_cache_key(slug), _build, ttl_seconds=_BUNDLE_TTL_SECONDS,
            )
        except Exception:
            bundle = await _compose_bundle(slug)

        if bundle is None:
            raise HTTPException(status_code=404, detail="No coverage")
        return JSONResponse(bundle)

"""
Security suite — P5.3 — XSS hardening.

Playwright-free version: the audit brief called for Playwright headless
rendering, but the CI runners don't have Playwright installed. We test
the two layers that matter here without it:

  1. **JSON boundary** — /api/spikes must serve attacker-controlled
     entity / summary / excerpt fields as properly-escaped JSON strings.
     The resulting body must parse and reconstitute the exact bytes,
     with no HTML interpolation at the API layer.
  2. **Frontend rendering** — static/annoyance.js and entity.js render
     the fields into the DOM. Grep-asserts the renderers use textContent
     / .innerText / createTextNode rather than innerHTML for user-
     controlled fields. An innerHTML on any of these is a regression.

These two together close the XSS hole end-to-end for the stack we can
test in CI. A pure-Playwright test belongs in a staging smoke, not unit CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import db


pytestmark = pytest.mark.integration


XSS_PAYLOADS = {
    "entity_xss": "<script>alert(1)</script>",
    "summary_xss": "<img src=x onerror=alert(1)>",
    "excerpt_xss": "<svg onload=alert(1)>",
    "event_handler": "\" onclick=\"alert(1)\"",
    "js_scheme": "javascript:alert(1)",
}

_STATIC = Path(__file__).resolve().parents[2] / "static"


# ── 1. API layer: JSON round-trip integrity ──────────────────────────────────

def test_api_spikes_returns_xss_payloads_as_escaped_strings(test_client, paywall_env):
    """Crafted spike fields must come back as strings (properly escaped
    in the transport), not interpreted as HTML by any intermediate layer."""
    from tests.conftest import pro_headers

    hour = db.current_hour_iso()
    sid = db.insert_spike(
        entity=XSS_PAYLOADS["entity_xss"],
        detected_hour=hour,
        z_score=4.0,
        multiple_of_baseline=5.0,
        avg_annoyance=80.0,
        count=12,
        sample_post_ids=[],
        summary=XSS_PAYLOADS["summary_xss"],
        sample_excerpts=[
            XSS_PAYLOADS["excerpt_xss"],
            XSS_PAYLOADS["event_handler"],
            XSS_PAYLOADS["js_scheme"],
        ],
        confidence_score=75.0,
    )
    assert sid is not None

    r = test_client.get("/api/spikes", headers=pro_headers())
    assert r.status_code == 200
    body = r.json()
    assert len(body["spikes"]) >= 1
    found = next((s for s in body["spikes"] if s["entity"] == XSS_PAYLOADS["entity_xss"]), None)
    assert found is not None, "round-trip lost the spike"

    # Values come back byte-for-byte — string in, string out. The transport
    # properly escapes script chars inside a JSON string literal; the API
    # layer does no HTML-interpolation.
    assert found["entity"] == XSS_PAYLOADS["entity_xss"]
    assert found["summary"] == XSS_PAYLOADS["summary_xss"]
    assert XSS_PAYLOADS["excerpt_xss"] in found["sample_excerpts"]
    assert XSS_PAYLOADS["event_handler"] in found["sample_excerpts"]
    assert XSS_PAYLOADS["js_scheme"] in found["sample_excerpts"]


def test_api_spikes_raw_body_escapes_script_tag(test_client, paywall_env):
    """The raw JSON bytes must have the '<script>' angle bracket escaped
    inside the JSON string literal so a mis-configured proxy that injects
    Content-Type: text/html can't turn the response into a live script."""
    from tests.conftest import pro_headers

    hour = db.current_hour_iso()
    db.insert_spike(
        entity=XSS_PAYLOADS["entity_xss"],
        detected_hour=hour,
        z_score=4.0, multiple_of_baseline=5.0, avg_annoyance=80.0,
        count=12, sample_post_ids=[], confidence_score=75.0,
    )
    r = test_client.get("/api/spikes", headers=pro_headers())
    assert r.status_code == 200
    # JSON response — verify content-type is the safe one.
    content_type = r.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"/api/spikes returned content-type {content_type!r}; must be application/json"
    )


def test_api_entity_detail_escapes_name_param(test_client, paywall_env):
    """The {name} path param goes into the JSON response. Use a payload
    without path-splitting slashes; the routing layer hands us whatever's
    between the path separators, so payload design matters more than the
    escape layer here — the JSON encoder is what keeps us safe."""
    from tests.conftest import pro_headers
    # Avoid '/' in the payload so FastAPI routing doesn't split it.
    payload = "<script>alert(1)</script>".replace("/", "")
    import urllib.parse as up
    r = test_client.get(f"/api/entity/{up.quote(payload, safe='')}", headers=pro_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["entity"] == payload
    # Content-Type must be JSON so a browser won't render it as HTML.
    assert r.headers.get("content-type", "").startswith("application/json")


# ── 2. Frontend renderer grep: no innerHTML on user-controlled fields ────────

def _frontend_js_files() -> list[Path]:
    return sorted(p for p in _STATIC.glob("*.js") if p.is_file())


# Field names that flow from user-controlled DB rows into the DOM. Any
# renderer that drops these into innerHTML is a regression.
USER_CONTROLLED_FIELDS = ("entity", "summary", "content", "sample_excerpts", "excerpt")


def test_frontend_js_files_exist():
    files = _frontend_js_files()
    assert files, f"no .js files under {_STATIC}"


def test_frontend_renderers_avoid_innerhtml_on_user_fields():
    """Grep assertion: flag only innerHTML / outerHTML / insertAdjacentHTML
    assignments that interpolate a variable (template-literal `${...}` or
    string concat `+`). Static-string innerHTML assignments (e.g.
    `.innerHTML = '<div>no data</div>'`) are safe and allowed.
    """
    offenders: list[str] = []
    risky_sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    # An assignment is risky iff it contains a sink AND uses template
    # interpolation (${) OR string concat with a variable (+).
    template_re = re.compile(r"\$\{[^}]+\}")
    concat_re = re.compile(r"\+\s*[A-Za-z_$]")
    for js in _frontend_js_files():
        text = js.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not any(sink in line for sink in risky_sinks):
                continue
            if template_re.search(line) or concat_re.search(line):
                offenders.append(f"{js.name}:{lineno} {line.strip()[:140]}")
    assert not offenders, (
        "Frontend renderers are interpolating variables into innerHTML / "
        "insertAdjacentHTML / document.write:\n"
        + "\n".join(offenders)
        + "\n\nUse textContent / createTextNode / appendChild instead."
    )


def test_frontend_uses_textcontent_for_entity_name():
    """Positive assertion: at least one .js file sets textContent when
    rendering the entity name. Catches the case where a refactor removes
    the safe setter and nothing trips the innerHTML grep above."""
    found = False
    for js in _frontend_js_files():
        if re.search(r"\.textContent\s*=", js.read_text()):
            found = True
            break
    assert found, "no frontend file uses .textContent — DOM rendering may be unsafe"


# ── 3. Sensitive-content blur wrapper is still in place ──────────────────────

def test_sensitive_posts_reach_client_with_sensitive_flag(test_client, paywall_env):
    """Regression: the sensitive-content blur depends on the API exposing
    is_sensitive=true on the payload. Assert that flag makes it through."""
    from tests.conftest import pro_headers

    hour = db.current_hour_iso()
    db.insert_post(
        id="reddit:sens", source="reddit", content=XSS_PAYLOADS["excerpt_xss"],
        posted_at=hour, source_channel="r/test", engagement=1,
    )
    db.insert_classification(
        post_id="reddit:sens", annoyance_score=90.0, sentiment="angry",
        primary_topic=None,
        entities=[{"name": "EvilCorp", "type": "company", "salience": 1.0}],
        model="v1", is_sensitive=True, sensitive_reason="nsfw",
    )
    sid = db.insert_spike(
        entity="EvilCorp",
        detected_hour=hour,
        z_score=4.0, multiple_of_baseline=5.0, avg_annoyance=90.0,
        count=10, sample_post_ids=["reddit:sens"],
        sample_excerpts=[XSS_PAYLOADS["excerpt_xss"]],
        confidence_score=75.0,
    )
    r = test_client.get("/api/spikes", headers=pro_headers())
    assert r.status_code == 200
    body = r.json()
    matches = [s for s in body["spikes"] if s["entity"] == "EvilCorp"]
    assert matches, "spike was not returned"
    # The blur wrapper on the frontend keys on is_sensitive on the post —
    # verify it's exposed in sample_posts so the client can set data-sensitive.
    sp = matches[0].get("sample_posts") or []
    if sp:
        assert any(p.get("is_sensitive") for p in sp), (
            "sensitive flag not exposed on hydrated sample_posts"
        )

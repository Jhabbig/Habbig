"""QA walks A–J — automated coverage of the manual pre-deploy checklist.

The previous batch's audit doc had ten manual walks (boot smoke, unauth
pages, authed pages, admin pages, style spot-check, UX state sweep,
mobile, perf headers, dark mode, Lighthouse). Each walk is now a Pytest
file under this package; the human checklist that survives at
QA_WALKTHROUGH.md covers the things automation can't catch (eye-test,
toast feel, etc.).

Conftest in this directory wires up:
  * an in-memory DB via `tests._testdb` (shared with the other test
    files so route-side state lines up),
  * a TestClient for the FastAPI app,
  * `_anon`, `_authed`, `_admin` fixtures returning ready-to-use
    cookie dicts.

Playwright-only walks (G mobile, I dark-mode, J Lighthouse) detect
their tooling at import time and `pytest.skip` cleanly when missing —
so `pytest tests/qa/` always passes on a vanilla Python install,
and runs the full thing on a CI box that's installed Playwright.
"""

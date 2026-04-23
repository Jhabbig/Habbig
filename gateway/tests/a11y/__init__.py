"""Accessibility test suite.

Every page that sits on the public web surface is expected to pass:
  1. Static-HTML shape checks (landmark, lang, h1, skip link).
  2. Axe-core WCAG 2.1 AA validation (when ``@axe-core/cli`` is on PATH).

The static checks run in every CI job; the axe-core run is gated behind
an environment flag (``NARVE_RUN_AXE=1``) so we don't stall the test
suite on the ~30 s Chrome download when that tool isn't pre-installed.
"""

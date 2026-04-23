"""Playwright-driven cross-browser + cross-viewport regression suite.

Tests in this package exercise the shipped frontend on chromium, firefox,
and webkit, plus six synthetic viewports. They are collected by the
normal ``pytest`` runner but skip cleanly when ``playwright`` isn't
installed — CI machines that don't need a browser suite aren't forced
to install it.

To run the suite locally:

    python3 -m pip install playwright
    python3 -m playwright install --with-deps chromium firefox webkit
    python3 -m pytest tests/browser

Or via the one-shot helper::

    gateway/scripts/run-browser-tests.sh

See gateway/BROWSER_COMPAT.md for what's covered + manual QA checklist.
"""

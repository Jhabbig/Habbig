#!/usr/bin/env python3
"""Measure every clickable element on every public page at mobile width.

WCAG 2.1 Level AA, success criterion 2.5.5 — Target Size: pointer input
targets must be at least 44 × 44 CSS pixels (with narrow exceptions for
inline links in running text, etc., which we still check and let the
reviewer judge).

Uses Playwright under the hood so we measure the rendered size after the
page's own CSS + layout has run. Writes a tab-separated report to stdout
(or ``--json`` for machine-readable output).

Example:
    pip install playwright && playwright install chromium
    python3 scripts/a11y_touch_targets.py http://127.0.0.1:3000
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Iterable


MIN_PX = 44  # WCAG 2.5.5 minimum edge length


# Selector for every interactive element — kept in sync with the role
# list axe-core uses internally for "target-size".
_INTERACTIVE = (
    "a[href], button, [role='button'], "
    "input:not([type='hidden']):not([type='text']):not([type='email']):not([type='password']), "
    "select, [role='checkbox'], [role='radio'], [role='switch'], "
    "summary, [onclick], [tabindex]:not([tabindex='-1'])"
)


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "playwright not installed. Run:\n"
            "    pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(2)


def audit_url(url: str, width: int = 375, height: int = 812) -> list[dict]:
    """Open ``url`` at a mobile viewport and return every interactive element
    whose bounding box is smaller than 44 × 44 px."""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    bad: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": width, "height": height})
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception as exc:
            browser.close()
            return [{"error": str(exc), "url": url}]
        # Evaluate entirely in the browser so each element's getBoundingClientRect
        # is accurate AFTER layout. Single round-trip.
        raw = page.evaluate(
            """(selector) => {
                const out = [];
                document.querySelectorAll(selector).forEach((el) => {
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return;
                    if (r.width < %d || r.height < %d) {
                        out.push({
                            selector: (el.tagName.toLowerCase()
                                + (el.id ? ('#' + el.id) : '')
                                + (el.className && typeof el.className === 'string'
                                    ? '.' + el.className.split(/\\s+/).filter(Boolean).slice(0, 2).join('.')
                                    : '')
                            ).slice(0, 120),
                            w: Math.round(r.width * 10) / 10,
                            h: Math.round(r.height * 10) / 10,
                            href: el.href || null,
                            text: (el.innerText || '').slice(0, 60),
                        });
                    }
                });
                return out;
            }"""
            % (MIN_PX, MIN_PX),
            _INTERACTIVE,
        )
        browser.close()
        return [dict(url=url, **row) for row in raw]


def audit_urls(urls: Iterable[str]) -> list[dict]:
    findings: list[dict] = []
    for url in urls:
        findings.extend(audit_url(url))
    return findings


def _load_urls(base: str) -> list[str]:
    """Invoke list_public_urls.py for the list. Isolated so a caller can
    sub in a curated list when debugging one page."""
    script = str(sys.argv[0])
    script_dir = "/".join(script.split("/")[:-1]) or "."
    result = subprocess.run(
        [sys.executable, f"{script_dir}/list_public_urls.py", base],
        capture_output=True, text=True, check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:7000",
                        help="Base URL (default http://127.0.0.1:7000)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of TSV")
    args = parser.parse_args()

    urls = _load_urls(args.base)
    findings = audit_urls(urls)

    if args.json:
        print(json.dumps(findings, indent=2))
        return 1 if findings else 0

    if not findings:
        print("no touch-target violations (all interactive elements ≥ 44×44px)")
        return 0

    print("url\tselector\tw\th\ttext")
    for f in findings:
        if "error" in f:
            print(f"{f['url']}\tERROR\t-\t-\t{f['error']}")
        else:
            print(f"{f['url']}\t{f['selector']}\t{f['w']}\t{f['h']}\t{f['text']!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

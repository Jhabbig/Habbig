"""Anchor-integrity check for the long-form legal pages.

terms.html and privacy.html both carry a table-of-contents with ~30+
``href="#sN"`` links to section-level ``id="sN"`` anchors. A silent typo in
either list scrolls the reader nowhere and looks amateur in an audit.

This test parses both pages, extracts every ``href="#..."`` fragment and every
``id="..."`` anchor, and fails if they don't line up. Covers:

* Every TOC link points to an existing ``id``.
* Every ``id`` is referenced from at least one TOC link (catches orphan
  sections that the next rewrite forgot to wire into the TOC).
* The expected number of sections is present (guards against accidental
  truncation during a merge).

Cheap: pure file read + regex, no server boot, no DB.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


GATEWAY_ROOT = Path(__file__).resolve().parent.parent
TERMS_PATH = GATEWAY_ROOT / "static" / "terms.html"
PRIVACY_PATH = GATEWAY_ROOT / "static" / "privacy.html"

_HREF_RE = re.compile(r'href="#([^"]+)"')
_ID_RE = re.compile(r'id="([^"]+)"')


def _hrefs_and_ids(path: Path) -> tuple[set[str], set[str]]:
    text = path.read_text(encoding="utf-8")
    # We only care about in-page anchor hrefs — mailto: and https: href values
    # get filtered out naturally by the "#..." regex. Same for ids.
    return set(_HREF_RE.findall(text)), set(_ID_RE.findall(text))


class TestTermsAnchors(unittest.TestCase):
    def test_every_toc_href_has_target(self):
        hrefs, ids = _hrefs_and_ids(TERMS_PATH)
        missing = hrefs - ids
        self.assertFalse(
            missing,
            f"terms.html: TOC links to sections that don't exist: {sorted(missing)}",
        )

    def test_no_orphan_section_ids(self):
        hrefs, ids = _hrefs_and_ids(TERMS_PATH)
        # Only check section-numbered ids (sN). Some ids are for non-TOC anchors
        # e.g. footnote targets — ignore those via the sN prefix filter.
        section_ids = {i for i in ids if re.fullmatch(r"s\d+", i)}
        orphan = section_ids - hrefs
        self.assertFalse(
            orphan,
            f"terms.html: sections with no TOC entry: {sorted(orphan)}",
        )

    def test_section_count_meets_floor(self):
        """At least 30 numbered sections. Catches accidental truncation."""
        _, ids = _hrefs_and_ids(TERMS_PATH)
        section_ids = {i for i in ids if re.fullmatch(r"s\d+", i)}
        self.assertGreaterEqual(
            len(section_ids), 30,
            f"terms.html: only {len(section_ids)} numbered sections (want >= 30)",
        )


class TestPrivacyAnchors(unittest.TestCase):
    def test_every_toc_href_has_target(self):
        hrefs, ids = _hrefs_and_ids(PRIVACY_PATH)
        missing = hrefs - ids
        self.assertFalse(
            missing,
            f"privacy.html: TOC links to sections that don't exist: {sorted(missing)}",
        )

    def test_no_orphan_section_ids(self):
        hrefs, ids = _hrefs_and_ids(PRIVACY_PATH)
        section_ids = {i for i in ids if re.fullmatch(r"s\d+", i)}
        orphan = section_ids - hrefs
        self.assertFalse(
            orphan,
            f"privacy.html: sections with no TOC entry: {sorted(orphan)}",
        )

    def test_section_count_meets_floor(self):
        _, ids = _hrefs_and_ids(PRIVACY_PATH)
        section_ids = {i for i in ids if re.fullmatch(r"s\d+", i)}
        self.assertGreaterEqual(
            len(section_ids), 25,
            f"privacy.html: only {len(section_ids)} numbered sections (want >= 25)",
        )


class TestCrossPageLinks(unittest.TestCase):
    """Spot-check a few hand-wired cross-page links exist as IDs on the target."""

    def test_privacy_links_to_dpa_subprocessors(self):
        """privacy.html links to /dpa#subprocessors — make sure dpa.html has it."""
        dpa_text = (GATEWAY_ROOT / "static" / "dpa.html").read_text(encoding="utf-8")
        privacy_text = PRIVACY_PATH.read_text(encoding="utf-8")
        if "/dpa#subprocessors" not in privacy_text:
            self.skipTest("privacy.html doesn't reference /dpa#subprocessors")
        self.assertIn(
            'id="subprocessors"',
            dpa_text,
            "privacy.html links to /dpa#subprocessors but dpa.html has no such anchor",
        )

    def test_privacy_links_to_dpa_representatives(self):
        dpa_text = (GATEWAY_ROOT / "static" / "dpa.html").read_text(encoding="utf-8")
        privacy_text = PRIVACY_PATH.read_text(encoding="utf-8")
        for frag in ("eu-representative", "uk-representative"):
            ref = f"/dpa#{frag}"
            if ref not in privacy_text:
                continue
            self.assertIn(
                f'id="{frag}"',
                dpa_text,
                f"privacy.html links to {ref} but dpa.html has no such anchor",
            )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Scaffold ``entity_markets.json`` from ``config.ALIASES``.

Every canonical entity (the *values* of ALIASES, deduped) gets a single
placeholder entry pointing at ``https://narve.ai/markets/search?q=...``.
Curators replace these with real narve.ai market URLs post-merge; the
script only needs to re-run when new entities are added to ALIASES.

Usage::

    python3 scripts/build_entity_markets.py        # write entity_markets.json
    python3 scripts/build_entity_markets.py --check  # exit non-zero if stale
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

# Make `import config` work when the script is run from any directory.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402 — path adjusted above


OUT_PATH = _ROOT / "entity_markets.json"


def build() -> dict:
    """Return the dict that should be written to entity_markets.json.

    Placeholder links point at the local entity page's markets fragment
    (``/entity/{entity}#markets``) rather than narve.ai's `/markets/search`
    because that upstream route currently redirects to the site-access
    gate (302 → /gate → 404 for visitors without a session). Curators
    should replace these with real narve.ai market URLs as they come
    online. Until then the local link is a dead-end-but-not-broken
    landing that keeps the spike card's "View markets" expand working.
    """
    entities = sorted({v for v in config.ALIASES.values() if v})
    out: dict[str, list[dict]] = {}
    for e in entities:
        out[e] = [
            {
                "title": "No curated markets yet — click to suggest one",
                "url": f"/entity/{urllib.parse.quote(e)}#markets",
                "source": "placeholder",
            }
        ]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the generated JSON would differ from the file on disk. "
             "Use in CI to catch 'new ALIAS entries without a regen'.",
    )
    args = ap.parse_args()

    desired = build()
    serialized = json.dumps(desired, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if args.check:
        if not OUT_PATH.exists():
            print(f"::check:: {OUT_PATH} missing — run this script without --check")
            return 1
        current = OUT_PATH.read_text()
        if current != serialized:
            print(
                f"::check:: {OUT_PATH} is stale "
                f"(len(current)={len(current)}, len(expected)={len(serialized)}). "
                "Run: python3 scripts/build_entity_markets.py"
            )
            return 1
        print(f"::check:: OK — {len(desired)} entities match on-disk JSON")
        return 0

    OUT_PATH.write_text(serialized)
    print(f"Wrote {len(desired)} entities to {OUT_PATH}")
    # Show a handful so the operator can eyeball the output
    sample = list(desired.items())[:5]
    for name, entries in sample:
        print(f"  {name:30s} → {entries[0]['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

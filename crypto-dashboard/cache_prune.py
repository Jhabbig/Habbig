#!/usr/bin/env python3
"""Prune stale kline cache files. Run manually: python3 cache_prune.py [--days N] [--dry-run]

Deletes cache/*.json and cache/*.pkl whose date-range stem ended more than
N days ago (default 120). Files without a parseable _YYYYMMDD_YYYYMMDD stem
are left untouched. A .json is also removed immediately if a same-stem .pkl
exists (the pickle is the canonical parsed form).
"""

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
STEM_RE = re.compile(r"_(\d{8})_(\d{8})$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="keep ranges ending within N days")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cutoff = datetime.now() - timedelta(days=args.days)
    freed = 0
    for f in sorted(CACHE_DIR.glob("*")):
        if f.suffix not in (".json", ".pkl"):
            continue
        stem = f.stem
        m = STEM_RE.search(stem)
        stale = False
        if m:
            try:
                stale = datetime.strptime(m.group(2), "%Y%m%d") < cutoff
            except ValueError:
                pass
        redundant = f.suffix == ".json" and (CACHE_DIR / f"{stem}.pkl").exists()
        if stale or redundant:
            size = f.stat().st_size
            reason = "stale" if stale else "redundant (pkl exists)"
            print(f"{'would delete' if args.dry_run else 'deleting'}: {f.name} ({size / 1e6:.0f} MB, {reason})")
            if not args.dry_run:
                f.unlink()
            freed += size
    print(f"{'would free' if args.dry_run else 'freed'}: {freed / 1e9:.2f} GB")


if __name__ == "__main__":
    main()

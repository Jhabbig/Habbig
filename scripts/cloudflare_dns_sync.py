#!/usr/bin/env python3
"""Idempotent DNS sync for narve.ai subproduct subdomains.

Reads gateway/config.json -> list of subdomains. Compares against
Cloudflare's current DNS records. Reports diffs. With --apply, creates
missing records.

USAGE:
    python3 scripts/cloudflare_dns_sync.py            # dry-run
    python3 scripts/cloudflare_dns_sync.py --apply    # apply changes

ENV:
    CLOUDFLARE_API_TOKEN - scoped token with Zone:DNS:Edit on narve.ai
    CLOUDFLARE_ZONE_ID   - narve.ai zone identifier

Safety notes:
    - Default mode is DRY-RUN. The --apply flag must be explicit.
    - This script NEVER deletes DNS records. "Extra" records are
      surfaced for manual review only.
    - The API token must be scoped to Zone:DNS:Edit on the narve.ai
      zone (do not use a global "All zones" or account-wide token).
    - All HTTP calls have a 30s timeout to avoid hanging in CI.
"""
import argparse
import json
import os
import sys

import httpx

CF_BASE = "https://api.cloudflare.com/client/v4"
HTTP_TIMEOUT = 30.0  # seconds per request


def _repo_root() -> str:
    """Return the repo root (parent of the scripts/ dir holding this file)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_expected(config_path: str) -> set[str]:
    """Load the expected set of subdomain FQDNs from gateway/config.json."""
    with open(config_path) as f:
        cfg = json.load(f)
    return {f"{d['subdomain']}.narve.ai" for d in cfg["dashboards"].values()}


def _fetch_current(zone: str, headers: dict) -> dict:
    """Fetch current A/AAAA/CNAME records for the zone, keyed by name."""
    r = httpx.get(
        f"{CF_BASE}/zones/{zone}/dns_records?per_page=200",
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return {
        rec["name"]: rec
        for rec in r.json()["result"]
        if rec["type"] in ("A", "AAAA", "CNAME")
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Idempotent DNS sync for narve.ai subproduct subdomains.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create missing records. Default is dry-run (read-only).",
    )
    args = parser.parse_args()

    try:
        token = os.environ["CLOUDFLARE_API_TOKEN"]
        zone = os.environ["CLOUDFLARE_ZONE_ID"]
    except KeyError as exc:
        print(
            f"error: missing required env var {exc}. "
            "Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID.",
            file=sys.stderr,
        )
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    config_path = os.path.join(_repo_root(), "gateway", "config.json")
    expected = _load_expected(config_path)

    current = _fetch_current(zone, headers)

    missing = expected - current.keys()
    extra = {
        n for n in current.keys()
        if n.endswith(".narve.ai") and n != "narve.ai"
    } - expected

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Mode:     {mode}")
    print(f"Expected: {len(expected)} subdomain records")
    print(f"Current:  {len(current)} A/AAAA/CNAME records in zone")
    print(f"Missing:  {sorted(missing)}")
    print(f"Extra:    {sorted(extra)} (won't delete; review manually)")

    if not args.apply:
        if missing:
            print(
                "\nDry-run only. Re-run with --apply to create the "
                f"{len(missing)} missing record(s)."
            )
        else:
            print("\nAll expected records present. Nothing to do.")
        return 0

    if not missing:
        print("\nNothing to apply.")
        return 0

    failures = 0
    for name in sorted(missing):
        data = {
            "type": "CNAME",
            "name": name,
            "content": "narve.ai",
            "proxied": True,
            "ttl": 1,
        }
        r = httpx.post(
            f"{CF_BASE}/zones/{zone}/dns_records",
            headers=headers,
            json=data,
            timeout=HTTP_TIMEOUT,
        )
        ok = r.status_code in (200, 201)
        print(f"Creating {name}: HTTP {r.status_code} {'ok' if ok else 'FAIL'}")
        if not ok:
            failures += 1
            print(f"  -> {r.text[:300]}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

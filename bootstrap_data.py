#!/usr/bin/env python3
"""Data-readiness bootstrap for the narve.ai fleet. Stdlib only.

Run on the production box (or any machine with open egress):

    python3 bootstrap_data.py            # probe upstreams + audit keys + check services
    python3 bootstrap_data.py --refresh  # also pull real prices into financial-matrix-toolkit

Each dashboard fetches its own data at runtime, so "getting the data" means:
(1) every upstream reachable, (2) the few credentials set, (3) services up.
This script verifies all three and prints exactly what's missing.
See DATA_SOURCES.md for the full feed map.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
UA = os.environ.get("SEC_USER_AGENT", "narve.ai fleet bootstrap contact@narve.ai")
TIMEOUT = 12

GREEN, RED, YELLOW, DIM, NC = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"

# (label, probe URL, dashboards that depend on it)
UPSTREAMS = [
    ("Polymarket gamma",   "https://gamma-api.polymarket.com/markets?limit=1", "crypto, midterm, weather, sports, religion, truth, airace, centralbank"),
    ("Polymarket data",    "https://data-api.polymarket.com/trades?limit=1",   "top_traders"),
    ("Polymarket CLOB",    "https://clob.polymarket.com/markets?limit=1",      "crypto (books/liquidity), truth"),
    ("Kalshi",             "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1", "crypto, midterm, weather, sports, truth"),
    ("Binance spot",       "https://api.binance.com/api/v3/ping",              "crypto, crypto_trackers"),
    ("Binance perps",      "https://fapi.binance.com/fapi/v1/ping",            "crypto_trackers (funding)"),
    ("Yahoo Finance",      "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?range=1d&interval=1d", "stocks fallback, financial-matrix-toolkit"),
    ("Alpaca data",        "https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest", "stocks (needs keys; 401 = reachable)"),
    ("FRED CSV",           "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF", "centralbank, weather/climate, matrix-toolkit forecast"),
    ("SEC EDGAR",          "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001067983&type=13F&count=1", "whale"),
    ("CFTC CoT",           "https://publicreporting.cftc.gov/resource/6dca-aqww.json?$limit=1", "whale"),
    ("USGS quakes",        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson", "weather/disasters"),
    ("NWS API",            "https://api.weather.gov/alerts/active?limit=1",    "weather"),
    ("NASA EONET",         "https://eonet.gsfc.nasa.gov/api/v3/events?limit=1", "weather/disasters"),
    ("NOAA SPC",           "https://www.spc.noaa.gov/products/spcwwrss.xml",   "weather/disasters"),
    ("GDACS",              "https://www.gdacs.org/xml/rss.xml",                "weather/disasters"),
    ("The Odds API",       "https://api.the-odds-api.com/v4/sports/?apiKey=probe", "sports (401 = reachable, key needed)"),
    ("ESPN",               "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", "sports schedules"),
    ("DeFiLlama",          "https://api.llama.fi/v2/chains",                   "crypto_trackers"),
    ("Fear & Greed",       "https://api.alternative.me/fng/",                  "crypto_trackers"),
    ("Frankfurter FX",     "https://api.frankfurter.dev/v1/latest",            "centralbank"),
    ("HuggingFace",        "https://huggingface.co/api/models?limit=1",        "airace"),
    ("Celestrak",          "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle", "world (satellites)"),
    ("BBC RSS",            "https://feeds.bbci.co.uk/news/world/rss.xml",      "world, religion"),
    ("Vatican press",      "https://press.vatican.va/content/salastampa/en.html", "religion"),
    ("Stock-watcher S3",   "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json", "whale (congress)"),
    ("World Bank",         "https://api.worldbank.org/v2/country?format=json&per_page=1", "voters"),
    ("Anthropic API",      "https://api.anthropic.com/v1/models",              "truth LLM extraction (401 = reachable, key needed)"),
]

# (env var, requirement, gates-what)
CREDENTIALS = [
    ("GATEWAY_SSO_SECRET",    "required",  "SSO-gated dashboards answer 503 without it"),
    ("SEC_USER_AGENT",        "free",      "whale EDGAR ingest — just a 'Name email' string, set it now"),
    ("ODDS_API_KEY",          "signup",    "sports signal feed (the-odds-api.com, free tier)"),
    ("ANTHROPIC_API_KEY",     "signup",    "truth LLM extraction stage (regex-only without)"),
    ("TWITTER_BEARER_TOKEN",  "signup",    "truth + world X scraping"),
    ("TRUTHSOCIAL_USERNAME",  "signup",    "truth TruthSocial scraping"),
    ("ALPACA_API_KEY",        "signup",    "stock desk live quotes (free paper account)"),
    ("OPENFIGI_API_KEY",      "optional",  "whale CUSIP resolution, 10x faster with key"),
    ("X_BEARER_TOKEN",        "optional",  "world-state X overlay"),
    ("REDIS_PASSWORD",        "required",  "docker-compose redis"),
]


def load_env_files() -> dict:
    """Best-effort parse of the shared gateway env files (production first,
    local .env overrides), merged under the live environment."""
    merged: dict[str, str] = {}
    for name in ("gateway/.env.production", "gateway/.env"):
        p = ROOT / name
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            merged[k.strip()] = v.strip().strip('"').strip("'")
    merged.update({k: v for k, v in os.environ.items()})
    return merged


def probe(url: str) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # Any HTTP answer means the host is reachable; 401/403 usually just
        # means "key required" or "UA policy", which is fine for a probe.
        return e.code < 500, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__


def check_services() -> list[tuple[str, int, str]]:
    cfg_path = ROOT / "gateway" / "config.json"
    rows = []
    if not cfg_path.exists():
        return rows
    cfg = json.loads(cfg_path.read_text())
    for key, dash in cfg["dashboards"].items():
        if dash.get("merged_into"):
            continue
        port = dash["target"]
        ok, detail = probe(f"http://127.0.0.1:{port}/")
        rows.append((key, port, f"up ({detail})" if ok else "down"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh", action="store_true",
                    help="also run financial-matrix-toolkit's real-price refresh")
    args = ap.parse_args()

    print(f"\n{'='*72}\n narve.ai data bootstrap — upstreams, credentials, services\n{'='*72}")

    print(f"\n── Upstream feeds ({len(UPSTREAMS)}) " + "─" * 30)
    blocked = 0
    for label, url, used_by in UPSTREAMS:
        ok, detail = probe(url)
        mark = f"{GREEN}OK{NC}   " if ok else f"{RED}FAIL{NC} "
        blocked += 0 if ok else 1
        print(f"  {mark} {label:18s} {DIM}{detail:14s} → {used_by}{NC}")

    print(f"\n── Credentials (gateway/.env.production) " + "─" * 18)
    env = load_env_files()
    missing_signup = []
    for var, kind, gates in CREDENTIALS:
        if env.get(var):
            print(f"  {GREEN}SET{NC}     {var:22s} {DIM}{gates}{NC}")
        else:
            color = RED if kind == "required" else YELLOW
            tag = {"required": "MISSING", "free": "MISSING (free — set a string!)",
                   "signup": "MISSING (needs signup)", "optional": "unset (optional)"}[kind]
            print(f"  {color}{tag:8s}{NC} {var:22s} {DIM}{gates}{NC}")
            if kind == "signup":
                missing_signup.append(var)

    print(f"\n── Services (from gateway/config.json) " + "─" * 20)
    rows = check_services()
    down = [r for r in rows if r[2] == "down"]
    for key, port, state in rows:
        mark = f"{GREEN}●{NC}" if state != "down" else f"{RED}●{NC}"
        print(f"  {mark} {key:16s} :{port:<6} {state}")
    if down:
        print(f"  {DIM}start them: ./start_dashboards.sh restart  (or systemd/compose){NC}")

    if args.refresh:
        print(f"\n── financial-matrix-toolkit --refresh " + "─" * 21)
        r = subprocess.run([sys.executable, "main.py", "--refresh"],
                           cwd=ROOT / "financial-matrix-toolkit")
        print(f"  refresh exit code: {r.returncode}")

    print(f"\n{'='*72}\n Summary: {len(UPSTREAMS)-blocked}/{len(UPSTREAMS)} feeds reachable, "
          f"{len(down)}/{len(rows)} services down, "
          f"{len(missing_signup)} signup keys missing ({', '.join(missing_signup) or 'none'})\n"
          f" Everything reachable + running fetches its own data automatically.\n{'='*72}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

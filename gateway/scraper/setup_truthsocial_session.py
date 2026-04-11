#!/usr/bin/env python3
"""
Run once on the server to log into TruthSocial and save the session.
Launches a headed browser (requires display or VNC).

TruthSocial also has a public mode that works without login
for prominent accounts — this script sets up the authenticated
session for keyword search.

Usage:
    python scraper/setup_truthsocial_session.py

On a headless server:
    ssh -X user@server python scraper/setup_truthsocial_session.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.storage import db as store
from scraper.scrapers.truthsocial import TruthSocialScraper


async def main():
    store.init_db()
    scraper = TruthSocialScraper()
    await scraper.setup_session()


if __name__ == "__main__":
    asyncio.run(main())

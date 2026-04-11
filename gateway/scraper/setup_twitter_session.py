#!/usr/bin/env python3
"""
Run once on the server to log into X and save the session.
Launches a headed browser (requires display or VNC).

Usage:
    python scraper/setup_twitter_session.py

On a headless server, use VNC or X11 forwarding:
    ssh -X user@server python scraper/setup_twitter_session.py

Steps:
    1. Opens browser at twitter.com/login
    2. You log in manually (including 2FA if needed)
    3. Once logged in, press Enter in the terminal
    4. Saves cookies and localStorage to stealth/profiles/twitter/
    5. Validates session by checking the home timeline
"""

import asyncio
import sys
from pathlib import Path

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.storage import db as store
from scraper.scrapers.twitter import TwitterScraper


async def main():
    store.init_db()
    scraper = TwitterScraper()
    await scraper.setup_session()


if __name__ == "__main__":
    asyncio.run(main())

"""Scraper registry.

Each scraper module exposes:
    NAME: str               unique source identifier
    SECTION: str            one of models.SECTIONS
    REFRESH_SECONDS: int    polling cadence
    async def fetch() -> list[Item]

`registry()` returns the full list of (name, fetch, period) tuples for the
scheduler. Adding a new source = drop a new module in this directory and
import it below.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from models import Item

from . import (
    box_office,
    google_trends,
    instagram,
    kym,
    markets,
    music_charts,
    news,
    reddit_memes,
    spotify_charts,
    steam_top,
    substack,
    tiktok,
    urban_dictionary,
    wikipedia,
    x_trending,
    youtube_trending,
)

ScraperSpec = tuple[str, Callable[[], Awaitable[list[Item]]], int]


def registry() -> list[ScraperSpec]:
    modules = [
        tiktok, instagram, reddit_memes, kym,
        google_trends, wikipedia, youtube_trending, x_trending,
        box_office, music_charts, spotify_charts, steam_top,
        markets, news, urban_dictionary, substack,
    ]
    return [(m.NAME, m.fetch, m.REFRESH_SECONDS) for m in modules]

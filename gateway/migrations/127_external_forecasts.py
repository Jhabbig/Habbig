"""External forecast benchmarks — side-by-side probability comparison.

Two tables:

  ``external_forecasts`` — time series of (market, provider, probability).
  One row per (market, provider, recorded_at). Kept append-only by the
  sync job so the chart in the market detail page can render a line per
  provider over time.

  ``market_equivalences`` — cache of "which market on <provider> asks
  the same question as our market?". Populated by the Haiku-powered
  matcher in ``external_forecasts/matcher.py`` and reused for 90 days.
  Admin can override a bad match via ``/admin/equivalences``.

Four providers supported out of the gate:
  - ``metaculus``        — public API (/api2/questions/?search=)
  - ``manifold``         — public API (/v0/search-markets?term=)
  - ``fivethirtyeight``  — scraped (Next.js JSON embed)
  - ``silver_bulletin``  — scraped (Next.js JSON embed)

The UNIQUE constraint on ``(market_slug, provider, recorded_at)`` lets
the sync job be idempotent without relying on sqlite's ROWID.
"""

from __future__ import annotations


revision = "127"
down_revision = "126"


def upgrade(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS external_forecasts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            market_slug         TEXT NOT NULL,
            provider            TEXT NOT NULL,
            provider_market_id  TEXT,
            probability         REAL NOT NULL,
            recorded_at         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
            UNIQUE(market_slug, provider, recorded_at)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ext_forecast_market "
        "ON external_forecasts(market_slug, recorded_at DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ext_forecast_provider "
        "ON external_forecasts(provider, recorded_at DESC)"
    )

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_equivalences (
            market_slug         TEXT NOT NULL,
            provider            TEXT NOT NULL,
            provider_market_id  TEXT NOT NULL,
            provider_question   TEXT,
            confidence          REAL NOT NULL,
            mapped_by           TEXT NOT NULL DEFAULT 'auto',
            mapped_at           INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
            rejected            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(market_slug, provider)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_equiv_low_conf "
        "ON market_equivalences(confidence) WHERE rejected = 0"
    )


def downgrade(cur) -> None:
    cur.execute("DROP TABLE IF EXISTS market_equivalences")
    cur.execute("DROP TABLE IF EXISTS external_forecasts")

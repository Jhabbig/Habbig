"""Insider-disclosure signal aggregation (all public, all legal).

Six public data sources, each with its own fetcher in this package.
Every fetcher inherits the same contract so the scheduler, the admin
health page, and the correlator can treat them uniformly:

  BaseFetcher.fetch_once(limit=N) -> FetchResult
    Calls the upstream API, de-duplicates against insider_signals by
    (source, external_id), inserts new rows, updates insider_fetchers
    housekeeping.

  BaseFetcher.source_name: stable string used as the insider_signals.source
    and the insider_fetchers PK.

The correlator (insider/correlator.py) runs once per inserted signal,
asking Claude Sonnet which active markets could plausibly be affected
and with what confidence. Compute scoring is in insider/score.py.

Disclaimer — rendered on every public view of this data:
  "All data derived from mandatory public disclosures. narve.ai does not
   possess non-public information."
"""

from insider.base import BaseFetcher, FetchResult, SignalStrength, ALL_FETCHERS  # noqa: F401

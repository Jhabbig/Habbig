"""Pipeline layer: extract → categorise → resolve.

This package is the seam between the scraper side (which ingests raw
posts / market data) and the DB side (predictions + markets tables).
Entries into the pipeline live in ``pipeline.extract_step`` —
``process_post`` is the single call sites hit.
"""

from pipeline.extract_step import process_post, process_posts_batch  # noqa: F401

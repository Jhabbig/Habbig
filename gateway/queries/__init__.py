"""Per-domain query modules extracted from ``db.py``.

Importing this package is a no-op; call sites still reach the query
functions through ``db``, which re-exports everything from these
submodules. The split exists purely to keep ``db.py`` focused on
connection pooling + schema + migrations.
"""

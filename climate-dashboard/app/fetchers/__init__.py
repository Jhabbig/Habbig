"""Upstream data-source fetchers.

Each module here exposes:
  parse(text) -> structured data       # pure, used by tests
  fetch()     -> dict with metadata    # cached HTTP wrapper
plus URL / SOURCE / UNITS module constants.
"""

"""narve.ai API support package.

Currently hosts the deprecation plumbing used by the URL-versioning
middleware in `server.py`. The actual v1 route handlers still live
in top-level `api_v1.py` and the main `server.py` / `server_features.py`
modules (the middleware rewrites `/api/v1/...` → `/api/...` internally).
"""

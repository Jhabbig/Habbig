"""Climate dashboard core modules.

Layout:
  app.cache         in-memory TTL cache (per-source TTLs in app.cache.TTL)
  app.http          shared `requests.get` wrapper with our user-agent + logging
  app.math_utils    normal CDF + linear regression helpers

  app.fetchers.*    one module per upstream data source (NASA, NOAA, NSIDC…)
                    each exposes `fetch()` (cached) and a pure `parse(text)`.
  app.models.*      one module per prediction model (temperature, co2, …)
  app.methodology   structured human-readable description of each model,
                    served as JSON at /api/methodology
"""

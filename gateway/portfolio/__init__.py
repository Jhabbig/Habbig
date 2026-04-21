"""Portfolio integration — Polymarket + Kalshi connection + positions.

Submodules:

  polymarket  — wallet connect (public address) + position sync
  kalshi      — password connect (encrypted token) + position sync
  positions   — unified read API across both platforms
  kelly       — bet-sizing calculator
  routes      — HTTP routes registered via register(app)
  jobs        — ARQ sync + reconciliation jobs

Encryption: Kalshi bearer tokens are stored Fernet-encrypted using the
``CREDENTIALS_ENCRYPTION_KEY`` env var. If the key is missing in dev
the Kalshi connect endpoint returns a 503 rather than storing a
plaintext token.
"""

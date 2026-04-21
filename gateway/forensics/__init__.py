"""Forensic subsystem — data-level signing + watermark recovery.

Two modules share this namespace:

  - ``signer``   — server-side, applies per-user fingerprints to list-endpoint
                   JSON responses.
  - ``extract_watermark`` — offline recovery tool; walks known user seeds
                   and scores a suspected leaked image or payload.

Keeping them side-by-side is deliberate: the matcher has to speak exactly
the same language as the signer, and catching drift during review is
easier when they're in the same directory.
"""

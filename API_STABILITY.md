# narve.ai API Stability

## Versioning
- `/api/v1/*` — stable. Backward-compatible changes only.
- `/api/internal/*` — internal use; can change without notice.
- `/api/*` (no version) — DEPRECATED for new consumers; will sunset after v1 fully covers it.

## Breaking change policy
- 6-month deprecation notice via /changelog + email to API key owners
- `Sunset` HTTP header on deprecated endpoints
- Old version stays live for 12 months after deprecation

## Authentication
- Public read endpoints: no auth or API key (rate-limited)
- Authenticated endpoints: session cookie + CSRF
- Embed endpoints: X-API-Key
- Subproduct subdomain endpoints: HMAC X-Gateway-Secret (server-to-server)

## SLA
- 99.5% uptime target
- Response time: p99 < 500ms for read endpoints
- Bulk endpoints rate-limited per spec

## Endpoint stability matrix

| Endpoint | Version | Stability |
|---|---|---|
| /api/v1/markets | v1 | stable |
| /api/v1/sources/{handle} | v1 | stable |
| /api/v1/predictions/{id} | v1 | stable |
| /api/v1/feed | v1 | experimental — may change shape before 2026-12 |
| /api/embed/* | v1 | stable |
| /api/me/* | unversioned | stable — session auth, web-app use only |

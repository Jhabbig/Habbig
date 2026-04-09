# gateway/static/dummy/ — Placeholder index

A one-file placeholder used as a default `root_path` target so the
gateway has *something* to serve at `/dummy/` during local development —
useful when testing subdomain routing without wiring up a real
dashboard backend.

## Files in this directory

| File | Purpose |
|---|---|
| `index.html` | Trivial "this is a dummy page" HTML stub — exists so `/dummy/` returns 200 instead of 404. Not used in production. |

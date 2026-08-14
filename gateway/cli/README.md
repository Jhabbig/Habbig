# narve — gateway engine CLI

Command-line interface for the whole narve.ai engine: the dashboard fleet,
users, subscriptions, invite tokens, revenue, enquiries, and the database
layer. Stdlib-only Python; talks to the same `gateway/db.py` data layer as the
server (honouring `DATABASE_URL` for PostgreSQL, SQLite otherwise) and reads
`gateway/config.json` as the source of truth for the fleet.

```
./narve fleet                # fleet table: state, port, health probe, subs, run-rate
./narve health               # probe the gateway + every deployed dashboard port
./narve dashboards [KEY]     # storefront config, per-dashboard detail
./narve users list|show|create|suspend|unsuspend|promote|demote|role|delete
./narve subs [--user IDENT]  # subscriptions, optionally per user
./narve grant IDENT KEY      # grant a subscription  (--plan, --days, KEY or 'all')
./narve revoke IDENT KEY
./narve tokens [create|revoke]
./narve revenue              # active counts + monthly run-rate per dashboard
./narve enquiries [--read ID]
./narve db [stats|init]
./narve config
```

Users are addressed by id, email, or username interchangeably. Every command
accepts `--json` (machine-readable) and `--plain` (no ANSI) in any position;
output is automatically plain when piped or when `NO_COLOR` is set.

## Design system

This CLI is the terminal port of the narve.ai design system
(`gateway/static/tokens.css`): monochrome, typography-forward,
information-dense. Colour never carries information — in fact the CLI emits no
colour at all. Because the terminal's background theme belongs to the user,
the light/dark token tables map onto SGR *attributes*, which invert correctly
on any theme:

| CSS token                | Terminal rendering                              |
| ------------------------ | ----------------------------------------------- |
| `--text-primary`         | bold                                            |
| `--text-secondary`       | default foreground                              |
| `--text-tertiary`        | dim (meta, captions, table headers)             |
| `--text-quaternary`      | dim, decorative separators only (`·`)           |
| `--rank-1` … `--rank-4`  | inverse fill → bold → default → dim             |
| `--interactive-bg`       | inverse video                                   |
| `--border-ghost/subtle`  | light box-drawing rule `─`, dim                 |
| `--error-border`         | heavy rule `━` — border **weight**, not hue     |
| `--font-mono`            | the terminal itself; numerics right-aligned     |
| card-meta                | dim + UPPERCASE (letterspaced wordmark)         |

Carried-over usage rules:

- **No colour for hierarchy.** Badges (`LIVE`, `DOWN`, `SUSPENDED`, roles,
  subscription states) differ by weight/fill and label, never hue. The rank-1
  inverse fill is reserved for alerts so a healthy fleet renders quiet.
- **Errors by border weight.** Error banners use the heavy `━` rule; info and
  warning banners use the hairline `─`.
- **Destructive actions confirm, they don't colour.** `users delete` prompts
  interactively and refuses to run non-interactively without `--yes`.
- **Accessibility.** Styling degrades to plain text under `NO_COLOR`,
  `TERM=dumb`, `--plain`, or any non-TTY pipe, so output stays
  grep/screen-reader friendly.

All styling lives in `cli/ui.py` — the terminal `tokens.css`. Components
(table, badge, banner, kv, rule, empty state) compose from it; command code in
`cli/main.py` never emits a raw escape code, mirroring the "never hardcode a
hex" rule.

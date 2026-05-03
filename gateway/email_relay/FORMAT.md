# Email format cheat-sheet

Subject is the routing key. Body is either empty or extra detail appended to the prompt.

## To Claude

| Subject | What happens |
|---|---|
| `[<bot>] <prompt>`         | Fresh Claude session in that bot's working dir |
| `[list]`                   | List 20 most recent threads across all project dirs |
| `[list:<bot>]`             | List threads scoped to one bot |
| `[<query>] <prompt>`       | Resume a session — `<query>` is a UUID prefix (≥6 hex) **or** a substring of the thread's first message. Must match exactly one. |

## Bot keys

```
centralbank   climate    crypto      midterm   sports
stock         top_traders  voters    weather   world   world_health
```

(Source of truth: `bot_dirs.json`. Add a key there to register a new bot.)

## Examples

```
[centralbank] should I close the FOMC arb?
[weather] check florida snow markets
[list]
[list:weather]
[abc12345] continue debugging
[FOMC arb] yes close it
```

## What you get back

A reply in the same email thread, within ~POLL_INTERVAL seconds (default 60s).
The footer tells you the thread ID — copy it for next time:

```
─
[centralbank · new thread abc12345]
Reply with [abc12345] <prompt> to continue.
```

For resumed threads:

```
─
[resumed abc12345 · /Polymarket/centralbank-dashboard · matched on title-substr]
  title: should I close the FOMC arb?
```

## Bot → you (push notifications)

Any bot can email you an alert via:

```python
from gateway.email_relay.notify import notify
notify("centralbank", "FOMC arb opened", "Poly 0.62 / Kalshi 0.65 / size $400")
```

You get an email with subject `[centralbank] FOMC arb opened`. Reply in-thread to spawn a fresh session in that bot's dir, **or** edit the subject to `[<thread-id>] ...` to resume an existing thread.

## Rules to remember

- Subject **must** start with `[tag]` — anything else is dropped silently.
- Tag matching is case-insensitive (`[CENTRALBANK]` works).
- `Re:` and `Fwd:` prefixes are stripped, so replying in-thread keeps the same routing.
- Only emails from `AUTHORIZED_FROM` are processed. Everything else is ignored.
- Resume queries that match 0 threads → reply tells you. Match >1 → reply lists candidates with UUID prefixes for disambiguation.
- Outbound emails carry an `X-Email-Relay` header so they can't trigger the relay if they bounce back to your inbox.

## Failure modes you might hit

| Symptom | Cause | Fix |
|---|---|---|
| Email ignored, no reply | Sender not in `AUTHORIZED_FROM` | Add address to `.env` |
| Email ignored, no reply | Subject has no `[tag]` | Add a tag |
| Reply: "original cwd no longer exists" | Resumed thread was created in a worktree that's been deleted | Start a fresh session |
| Reply: "claude timed out after 300s" | Long-running task | Bump `CLAUDE_TIMEOUT` in `.env` |
| Reply: "Not logged in · run /login" | Relay process can't reach your Claude auth | Run `claude` interactively once on this machine; relaunch the relay |

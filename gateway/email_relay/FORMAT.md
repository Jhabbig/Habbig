# Email format cheat-sheet

Subject is the routing key. Body is either empty or extra detail appended to the prompt. Quoted prior-message text is stripped automatically — type your reply at the top, hit Send.

## The shortcut you'll use 95% of the time

**Just hit Reply.** Any reply to any Claude email continues that same conversation — no tag editing needed. You only ever set the `[tag]` when starting something new.

## To Claude

| Subject | What happens |
|---|---|
| `[<bot>] <prompt>`         | Fresh Claude session in that bot's working dir |
| `[<bot>:last] <prompt>`    | Resume the most recent thread in that bot's dir |
| `[<bot>:new] <prompt>`     | Force fresh, even if you're replying to a Claude email |
| `[<bot>:list]`             | List threads scoped to one bot |
| `[list]`                   | List 20 most recent threads across all project dirs |
| `[list:<bot>]`             | Alias of `[<bot>:list]` |
| `[<query>] <prompt>`       | Resume a specific session — `<query>` is a UUID prefix (≥6 hex) **or** a substring of the thread's first message. Must match exactly one. |

**Replying to a Claude email** continues that session automatically — no `[tag]` needed in your reply. The tag is only for *starting* a new conversation or *jumping* to a specific other one.

## Bot keys

```
centralbank   climate    crypto      midterm   sports
stock         top_traders  voters    weather   world   world_health
```

(Source of truth: `bot_dirs.json`. Add a key there to register a new bot.)

## Examples

```
[centralbank] should I close the FOMC arb?     ← start a fresh thread
[centralbank:last] continue debugging          ← jump back into the last centralbank thread
[centralbank:new] reset, ignore my last reply  ← force fresh during an active reply chain
[list]                                         ← see all your threads
[list:weather]                                 ← scope to weather
[abc12345] follow-up question                  ← resume by UUID prefix
[FOMC arb] yes close it                        ← resume by title substring
```

For ongoing chats, **just reply** — no subject editing.

## What you get back

A reply in the same email thread, within ~`POLL_INTERVAL` seconds (default 60s). The footer shows what happened:

```
─
[centralbank · new thread abc12345]
Reply to this email to continue the conversation.
```

For continued conversations:

```
─
[continued abc12345 · via In-Reply-To]
  title: should I close the FOMC arb?
```

For explicit resumes via subject tag:

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

You get an email with subject `[centralbank] FOMC arb opened`. **Just hit Reply** with your question — the relay starts a fresh centralbank session for that conversation, and any further replies in that thread continue it automatically.

## Rules to remember

- Replying to a Claude email always continues that session (powered by `In-Reply-To` → `threads.json` mapping).
- For new conversations, subject **must** start with `[tag]` — anything else is dropped silently.
- Tag matching is case-insensitive (`[CENTRALBANK]` works).
- `Re:` and `Fwd:` prefixes are stripped, so the subject tag still routes correctly.
- Only emails from `AUTHORIZED_FROM` are processed. Everything else is ignored.
- Resume queries that match 0 threads → reply tells you. Match >1 → reply lists candidates with UUID prefixes for disambiguation.
- Outbound emails carry an `X-Email-Relay` header so they can't trigger the relay if they bounce back to your inbox.
- Quoted prior-message text (Gmail's `On … wrote:` block, Apple Mail, Outlook `--- Original Message ---`) is stripped before the prompt is sent to Claude.

## Resume priority (when multiple signals conflict)

1. `[<bot>:new]` → always fresh (highest priority — your override switch)
2. `[<query>]` that resolves → resume that specific thread
3. `In-Reply-To` matches a known thread → continue that
4. `[<bot>]` or `[<bot>:last]` → fresh / resume-last
5. Otherwise → skip or error

## Failure modes you might hit

| Symptom | Cause | Fix |
|---|---|---|
| Email ignored, no reply | Sender not in `AUTHORIZED_FROM` | Add address to `.env` |
| Email ignored, no reply | New conversation with no `[tag]` in subject | Add a tag |
| Reply spawned a new thread instead of continuing | Replied to a non-Claude email (e.g. forwarded one), so `In-Reply-To` doesn't match | Use `[<bot>:last]` or `[<uuid>]` to specify |
| Reply: "original cwd no longer exists" | Resumed thread was created in a worktree that's been deleted | Start a fresh session |
| Reply: "claude timed out after 300s" | Long-running task | Bump `CLAUDE_TIMEOUT` in `.env` |
| Reply: "Not logged in · run /login" | Relay process can't reach your Claude auth | Run `claude` interactively once on this machine; relaunch the relay |

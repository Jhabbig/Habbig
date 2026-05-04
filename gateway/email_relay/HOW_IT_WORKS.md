# How the email relay works

A daemon that lets you drive Claude Code sessions from your phone, and lets your bots push alerts to your inbox. Stdlib only, ~400 lines total.

If you're returning to this code months later, this doc is the map.

---

## The two flows

### 1. You → Claude (you ask, Claude answers)

```
┌─ phone ──────────────────────┐
│ Send email to yourself:      │
│   Subject: [centralbank] foo │
│   Body:    (anything)        │
└─────────────┬────────────────┘
              ▼
        Gmail receives
              │
              ▼
┌─ relay.py (launchd / systemd) ────────────┐
│ every POLL_INTERVAL seconds:              │
│  1. IMAP SEARCH UID > last_seen           │
│  2. for each new message:                 │
│       a. has X-Email-Relay header? skip   │
│       b. From in AUTHORIZED_FROM? else skip│
│       c. parse_subject() → action         │
│       d. dispatch to handle_fresh /       │
│          handle_resume / handle_list      │
│       e. SMTP send_message reply          │
│       f. save_state(last_uid)             │
└─────────────┬─────────────────────────────┘
              ▼
       Reply lands in your
       Gmail thread on phone
```

### 2. Bot → you (push alert)

```
┌─ centralbank/poller.py ─────────────────┐
│ detects opportunity                     │
│                                         │
│ from gateway.email_relay.notify import …│
│ notify("centralbank", "FOMC arb opened",│
│        "Poly 0.62 / Kalshi 0.65")       │
└─────────────┬───────────────────────────┘
              ▼
       SMTP, with header
       X-Email-Relay: bot-push
              │
              ▼
       Lands in your inbox
              │
              ├─→ you read it
              │
              └─→ relay.py also sees it,
                  but X-Email-Relay header
                  → skip. No loop. ✓
```

---

## Subject grammar (the routing primitive)

Everything happens in the `[tag]` at the start of the subject line. Body is appended to the prompt as extra context (with quoted prior-message text stripped).

| Tag | Action | Why |
|---|---|---|
| `[<bot>]` | fresh session in `bot_dirs[bot]` | start a new conversation |
| `[<bot>:last]` | resume most recent session in that bot's dir | jump back into where you left off |
| `[<bot>:new]` | force fresh, override In-Reply-To | reset mid-thread |
| `[<bot>:list]` / `[list:<bot>]` | enumerate sessions for that bot | scoped variant |
| `[list]` | enumerate all sessions in `~/.claude/projects/` | "what threads have I got?" |
| `[<query>]` | resume session matching query | jump to a specific other thread |

The dispatcher in `parse_subject()` returns `{action: "skip" | "fresh" | "list" | "resume" | "resume_last", ...}`. `Re:` and `Fwd:` prefixes are stripped first so replies route the same way.

A query is a UUID prefix (≥6 hex chars, exact prefix match) or a substring of the session's title (case-insensitive). If 0 or >1 sessions match, the relay replies with an error or candidate list — it never silently picks one.

---

## Auto-continue on reply (the "feels like the desktop app" trick)

When the relay sends a reply, it includes a fresh `Message-ID` in the SMTP envelope and persists `Message-ID → session_id` to `threads.json`. When you hit Reply in Gmail, your reply's `In-Reply-To` header points to that exact `Message-ID`. The relay looks it up, finds the session, and resumes.

```
[user] [centralbank] FOMC arb status                  ← fresh
       └─ Message-ID: <reply-1@narve.ai>
          threads.json: {<reply-1@narve.ai>: abc12345}

[user replies] (subject: Re: [centralbank] FOMC arb status)
   In-Reply-To: <reply-1@narve.ai>                    ← maps to abc12345
   → resume abc12345
   └─ Message-ID: <reply-2@narve.ai>
      threads.json: {<reply-1>: abc12345, <reply-2>: abc12345}

[user replies again] (chains forever)
   In-Reply-To: <reply-2@narve.ai>                    ← also maps to abc12345
   → resume abc12345
```

`session_from_in_reply_to()` walks both `In-Reply-To` and the space-separated `References` header so deep reply chains don't need every intermediate Message-ID to be in the map — any one match resumes the right session.

### Priority when signals conflict

```
1. [<bot>:new]              → fresh                (explicit override)
2. [<query>] that resolves  → resume that          (explicit jump)
3. In-Reply-To match        → resume that          ← the auto-continue
4. [<bot>] / [<bot>:last]   → fresh / resume-last
5. [list] / [<bot>:list]    → list
6. fall-through             → skip / error
```

### Why this matters

Without auto-continue, every reply spawns a fresh session because `Re: [centralbank] foo` parses as `[centralbank]`. The user has to manually edit each subject to `[abc12345] foo` to maintain context — fine for one-shot questions, awful for back-and-forth. With In-Reply-To routing, replying just works, and the subject tag becomes "the address bar" — only used to navigate to a *different* conversation.

### Quoted text stripping

Mail clients prepend the prior message on Reply (`On Tue, May 3, 2026 at 7:14 PM you@gmail.com wrote: > foo > bar`). Without stripping, Claude sees the entire prior conversation re-pasted on every turn — confusing and wasteful. `strip_quoted()` cuts at:

- A `On <date> [at <time>] wrote:` line (Gmail / Apple Mail, EN/FR/ES/DE)
- The first `>` line that follows non-empty user content
- Outlook's `--- Original Message ---` separator

Top-quoted (user types above the quote) and bottom-quoted (rare) replies both work. Inline replies (interleaving `>` and new text) are partial — only the text above the first quote line is kept.

---

## Why custom headers, not subject markers, for loop guard

Every email the relay sends — both Claude replies and bot pushes — carries:

```
X-Email-Relay: claude-reply     (or "bot-push")
```

Inbound processing skips any message with this header set, regardless of sender. This is more robust than "skip if subject starts with [bot]" because:

- Subject text is user-editable; headers aren't (well, not by accident)
- Survives forward-and-reply chains
- Survives Gmail's threading rewrites

If the SMTP user and the IMAP user are the same Gmail address (the common case), bot pushes land back in the same inbox the relay reads. Without the header guard, every push would trigger a fresh Claude session, which would email back, which would... etc.

---

## Session resumption — the interesting part

Claude Code stores every session as `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`. Each line is a JSON event; the first user-typed message records `cwd`. We use this to:

1. **Enumerate**: `sessions.all_sessions()` globs `~/.claude/projects/*/*.jsonl`, opens each, extracts UUID (filename), title (first user message), cwd, mtime, and message count.
2. **Match**: `sessions.find_by_query(q)` tries exact UUID, then UUID prefix, then case-insensitive title substring.
3. **Resume**: `claude -p --resume <uuid> --output-format json` from the **session's original cwd** (so MCP servers, CLAUDE.md context, file paths all line up).

The `--output-format json` is critical — the parsed result has both `result` (the assistant's text) and `session_id` (the UUID, which we already have but it's a good sanity check that resume worked).

### Failure mode: cwd no longer exists

If the session lived in a worktree that's been deleted (`/Polymarket/.claude/worktrees/foo`), `pathlib.Path(s.cwd).is_dir()` returns False and we reply with an error. Worktree threads are unreachable; bot-dir threads (centralbank-dashboard, etc.) are stable.

---

## State

| Where | What | Why |
|---|---|---|
| `state.json` | `{"last_uid": <int>}` | Without this, restart re-processes the whole inbox |
| `threads.json` | `{<Message-ID>: <session_id>}` | Powers auto-continue on reply |
| `.env` | credentials, allowlist, timeouts | Loaded once at startup; restart to reload |
| `~/.claude/projects/` | session histories | Read-only from the relay's perspective |

State is persisted **after each message is processed**, not at end of poll cycle, so a crash mid-batch doesn't lose progress on already-handled messages. Failed processing still advances last_uid (so a poison message doesn't loop forever) — it just logs the error.

---

## Auth model

Two layers:

1. **Inbound auth (who can drive bots)**: `AUTHORIZED_FROM` is a comma-separated allowlist of email addresses. Anything else gets dropped with a log line. This is your first defense if someone learns your relay's email address.

2. **Claude auth (whether `claude -p` works)**: the relay process must inherit your Claude Code auth. On Mac, launchd jobs typically inherit your keychain, so once you've run `claude` interactively at least once, the daemon's invocations work. If you see "Not logged in · run /login" in replies, the daemon process can't reach your auth — fix by running `claude setup-token` and exporting the token in the relay's environment.

There's no second-factor on the email side — anyone who can spoof your address (or compromise your Gmail) can drive your bots. For now this is acceptable because the bots have read-only / dashboard scope; **never wire money-moving actions** into this without adding HMAC-signed subject lines or similar.

---

## Files

| File | Lines | Role |
|---|---|---|
| `relay.py` | ~290 | Main daemon: poll, parse, dispatch, reply |
| `sessions.py` | ~140 | Read-only enumeration of `~/.claude/projects/` |
| `notify.py` | ~55 | Bot-side helper to push an alert email |
| `bot_dirs.json` | — | Maps bot keys to working dirs |
| `.env.example` | — | Credential template |
| `FORMAT.md` | — | User-facing cheat sheet (the one you keep on your phone) |
| `HOW_IT_WORKS.md` | — | This doc |
| `com.narve.email-relay.plist` | — | launchd job (Mac) |
| `email-relay.service` | — | systemd unit (Ubuntu) |

`relay.py` only depends on stdlib + `sessions.py`. `notify.py` is fully standalone (bots import it without pulling in the relay). `sessions.py` is also standalone — you can `python3 -c "from sessions import all_sessions; print(len(all_sessions()))"` to inspect your sessions outside the relay.

---

## Operational details

### Polling cadence

`POLL_INTERVAL=60` (seconds) is the default. Gmail rate-limits aggressive IMAP polling — 60s is comfortably below any throttle. Going below 30s is asking for trouble. The user-perceived latency is ~30s on average (half the poll interval) plus however long `claude -p` takes (~10–60s typically).

### Claude timeout

`CLAUDE_TIMEOUT=300` (seconds). If a single invocation runs longer, it's killed and the user gets `error: claude timed out after 300s`. While Claude is running, the next poll is delayed (we're single-threaded). For typical use this is fine; if you want long agentic runs over email, refactor to spawn a subprocess and reply when it finishes.

### Concurrency

None. One Claude invocation at a time, sequentially. Two emails in the same poll cycle are processed in arrival order. The reason: parallel `claude -p` calls in the same cwd can race on file edits or session state. If you want concurrency, partition by bot dir.

### Logs

stdout/stderr go to `/tmp/email-relay.log` and `/tmp/email-relay.err` (configured in the launchd plist). On Ubuntu, `journalctl -u email-relay -f`. Set `LOG_LEVEL=DEBUG` in the env to see "skip: own outbound" lines etc.

---

## Common changes you might want to make

- **New bot**: add an entry to `bot_dirs.json`. No code change.
- **Different Gmail account / IMAP server**: edit `.env`. No code change.
- **Run on a server instead of laptop**: copy the dir, install Python 3, install `claude`, `claude setup-token`, set up the systemd unit.
- **Multi-recipient inbox routing**: change the `AUTHORIZED_FROM` check to pull from a JSON file mapping address → allowed bots.
- **Webhook trigger instead of IMAP polling**: replace `poll_once` with an HTTP handler. The dispatch logic in `process_message` is independent of how the message arrives.
- **HMAC subject signing**: prepend `[hmac:abc123def] [centralbank] foo` and verify before dispatch. Do this before wiring any money-moving actions.

---

## What this is NOT

- Not a Claude Code session manager (no UI, no tabs, no persistent attached sessions)
- Not transactional (a Claude reply that fails to send is logged and lost)
- Not multi-user (auth is single-tenant via `AUTHORIZED_FROM`)
- Not realtime (60s polling latency)
- Not token-aware (no spend caps; `claude -p` runs without `--max-budget-usd`)

If any of those start to matter, this stops being the right architecture.

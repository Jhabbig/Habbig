#!/usr/bin/env python3
"""
Email relay: poll Gmail via IMAP, route emails to Claude Code sessions, reply
in-thread with Claude's output. Designed to feel like the Claude desktop app:
hit "Reply" on any reply we sent, and the same session continues — no tag
fiddling needed.

Subject grammar (a fallback when In-Reply-To threading isn't available):
    [<bot>] <prompt>          fresh session in that bot's working dir
    [<bot>:last] <prompt>     resume most recent session in that bot's dir
    [<bot>:new] <prompt>      force fresh even if In-Reply-To matches
    [<bot>:list]              list threads scoped to that bot
    [list]                    list recent threads across all project dirs
    [list:<bot>]              alias of [<bot>:list]
    [<query>] <prompt>        resume by UUID prefix (≥6 hex chars) or by
                              case-insensitive title substring (must match one)

Resume priority (highest first):
    1. Subject explicitly says [<bot>:new]                  → fresh
    2. Subject is an explicit [<query>] that resolves       → resume that
    3. In-Reply-To header maps to a known session           → resume that
    4. Subject [<bot>] or [<bot>:last]                      → fresh / resume-last
    5. fall-through error / skip

That priority means: replying to any Claude reply continues the conversation
automatically, but you can always override with an explicit subject tag.

Auth:
    From must match AUTHORIZED_FROM (else dropped).
    X-Email-Relay header must be absent (loop guard for our own outbound).

State:
    state.json    — last IMAP UID (so restarts don't reprocess inbox)
    threads.json  — Message-ID → session_id mapping (powers auto-continue)
"""
from __future__ import annotations

import email
import imaplib
import json
import logging
import os
import pathlib
import re
import smtplib
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import parseaddr, make_msgid

import sessions

HERE = pathlib.Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
THREADS_FILE = HERE / "threads.json"
BOT_DIRS_FILE = HERE / "bot_dirs.json"
SUBJECT_RE = re.compile(r"\[([^\]]+)\]\s*(.*)", re.DOTALL)

# Gmail / Apple Mail / Outlook reply-quote markers. We cut the body at the
# first line that looks like a quote header so Claude doesn't see the prior
# conversation pasted back at it on every reply.
QUOTE_HEADER_RE = re.compile(
    r"""^(
          On\s+.+\s+wrote:\s*$         |  # English (Gmail, Apple Mail)
          On\s+.+\s+at\s+.+\s+wrote:\s*$|  # English (Apple Mail full form)
          Le\s+.+\s+a\s+écrit\s*:\s*$  |  # French
          El\s+.+\s+escribió:\s*$      |  # Spanish
          Am\s+.+\s+schrieb.+:\s*$     |  # German
          .+\s+<[^>]+@[^>]+>\s+wrote:\s*$ |  # "Name <addr@x> wrote:"
          [-]{2,}\s*Original\s+Message\s*[-]{2,}\s*$  # Outlook
       )$""",
    re.IGNORECASE | re.VERBOSE,
)

log = logging.getLogger("email_relay")


# ─── env / state ──────────────────────────────────────────────────────────

def load_env() -> dict:
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    required = ["IMAP_HOST", "IMAP_USER", "IMAP_PASS",
                "SMTP_HOST", "SMTP_USER", "SMTP_PASS",
                "AUTHORIZED_FROM"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)} (see .env.example)")

    return {
        "imap_host":       os.environ["IMAP_HOST"],
        "imap_port":       int(os.environ.get("IMAP_PORT", "993")),
        "imap_user":       os.environ["IMAP_USER"],
        "imap_pass":       os.environ["IMAP_PASS"],
        "imap_mailbox":    os.environ.get("IMAP_MAILBOX", "INBOX"),
        "smtp_host":       os.environ["SMTP_HOST"],
        "smtp_port":       int(os.environ.get("SMTP_PORT", "587")),
        "smtp_user":       os.environ["SMTP_USER"],
        "smtp_pass":       os.environ["SMTP_PASS"],
        "authorized_from": {a.strip().lower() for a in os.environ["AUTHORIZED_FROM"].split(",") if a.strip()},
        "poll_interval":   int(os.environ.get("POLL_INTERVAL", "60")),
        "claude_bin":      os.environ.get("CLAUDE_BIN", "claude"),
        "claude_timeout":  int(os.environ.get("CLAUDE_TIMEOUT", "300")),
    }


def load_bot_dirs() -> dict[str, pathlib.Path]:
    raw = json.loads(BOT_DIRS_FILE.read_text())
    return {k: (HERE / v).resolve() for k, v in raw.items() if not k.startswith("_")}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_uid": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_threads() -> dict[str, str]:
    """Message-ID → session_id mapping."""
    if THREADS_FILE.exists():
        return json.loads(THREADS_FILE.read_text())
    return {}


def save_threads(threads: dict[str, str]) -> None:
    THREADS_FILE.write_text(json.dumps(threads, indent=2))


def remember_thread(threads: dict, message_id: str, session_id: str) -> None:
    if not message_id or not session_id:
        return
    threads[message_id] = session_id
    save_threads(threads)


def session_from_in_reply_to(threads: dict, msg: email.message.Message) -> str | None:
    """Walk References and In-Reply-To, return the first matched session_id."""
    candidates = []
    if msg.get("In-Reply-To"):
        candidates.append(msg["In-Reply-To"].strip())
    refs = msg.get("References", "")
    candidates.extend(r.strip() for r in refs.split())
    for c in candidates:
        if c in threads:
            return threads[c]
    return None


# ─── parsing ──────────────────────────────────────────────────────────────

def parse_subject(subject: str, bot_keys: set[str]) -> dict:
    """
    Returns one of:
        {"action": "skip",    "reason": str}
        {"action": "fresh",   "bot": str, "force": bool}            # force=True for [bot:new]
        {"action": "resume_last", "bot": str}
        {"action": "list",    "bot": str | None}
        {"action": "resume",  "query": str}
    Note: "prompt" is no longer included here — caller extracts it from the
    cleaned subject remainder + body.
    """
    cleaned = re.sub(r"^((re|fwd|fw):\s*)+", "", subject, flags=re.IGNORECASE)
    m = SUBJECT_RE.match(cleaned.strip())
    if not m:
        return {"action": "skip", "reason": "no [tag] in subject"}
    tag = m.group(1).strip()
    tag_lower = tag.lower()

    # global list
    if tag_lower == "list":
        return {"action": "list", "bot": None}
    if tag_lower.startswith("list:"):
        return {"action": "list", "bot": tag_lower.split(":", 1)[1].strip()}

    # bot:command forms
    if ":" in tag_lower:
        left, right = (s.strip() for s in tag_lower.split(":", 1))
        if left in bot_keys:
            if right == "list":
                return {"action": "list", "bot": left}
            if right == "last":
                return {"action": "resume_last", "bot": left}
            if right == "new":
                return {"action": "fresh", "bot": left, "force": True}
            # bot:something-else — treat as resume query scoped to bot? for now
            # just fall through to global resume on the right-hand side.
            return {"action": "resume", "query": right}

    # single-token bot key → fresh (in-reply-to may override later)
    if tag_lower in bot_keys:
        return {"action": "fresh", "bot": tag_lower, "force": False}

    # everything else: free-form resume query
    return {"action": "resume", "query": tag}


def subject_remainder(subject: str) -> str:
    """The text after the [tag], for use as a prompt."""
    cleaned = re.sub(r"^((re|fwd|fw):\s*)+", "", subject, flags=re.IGNORECASE)
    m = SUBJECT_RE.match(cleaned.strip())
    return m.group(2).strip() if m else ""


def strip_quoted(body: str) -> str:
    """
    Remove the quoted prior message that mail clients prepend on Reply.
    Cut at the first quote-header line OR the first line that begins with
    '>' (after the user's own text). Keeps everything above.
    """
    lines = body.splitlines()
    out = []
    for line in lines:
        if QUOTE_HEADER_RE.match(line):
            break
        # consecutive '>' lines = quoted block; cut at the first one if we
        # already have some content above (otherwise it's a top-quoted reply,
        # which we treat as no fresh content).
        if line.lstrip().startswith(">"):
            if out and any(o.strip() for o in out):
                break
    out.append(line) if False else None  # noop, keep linter quiet
    # Actually rebuild properly:
    out = []
    for line in lines:
        if QUOTE_HEADER_RE.match(line):
            break
        if line.lstrip().startswith(">") and out and any(o.strip() for o in out):
            break
        out.append(line)
    return "\n".join(out).strip()


def extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


# ─── claude invocation ────────────────────────────────────────────────────

def _run_claude(cmd: list[str], cwd: pathlib.Path, timeout: int) -> tuple[str, str]:
    """Run claude with --output-format json. Returns (response_text, session_id)."""
    log.info("claude exec: cwd=%s argv=%s", cwd, " ".join(cmd[1:]))
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON: {proc.stdout[:500]}")
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {data.get('result', 'unknown')}")
    return data.get("result", ""), data.get("session_id", "")


def invoke_fresh(cwd: pathlib.Path, prompt: str, claude_bin: str, timeout: int) -> tuple[str, str]:
    return _run_claude([claude_bin, "-p", prompt, "--output-format", "json"], cwd, timeout)


def invoke_resume(cwd: pathlib.Path, prompt: str, session_id: str, claude_bin: str, timeout: int) -> tuple[str, str]:
    return _run_claude(
        [claude_bin, "-p", prompt, "--resume", session_id, "--output-format", "json"],
        cwd, timeout,
    )


# ─── reply ────────────────────────────────────────────────────────────────

def send_reply(cfg: dict, threads: dict, orig: email.message.Message,
               body: str, tag: str, session_id: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = cfg["smtp_user"]
    msg["To"] = orig.get("Reply-To") or orig.get("From")
    orig_subject = orig.get("Subject", "")
    msg["Subject"] = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    if orig.get("Message-ID"):
        msg["In-Reply-To"] = orig["Message-ID"]
        msg["References"] = (orig.get("References", "") + " " + orig["Message-ID"]).strip()
    new_message_id = make_msgid(domain=cfg["smtp_user"].split("@")[-1])
    msg["Message-ID"] = new_message_id
    msg["X-Email-Relay"] = "claude-reply"
    msg["X-Bot-Key"] = tag
    if session_id:
        msg["X-Claude-Session-Id"] = session_id  # informational; not relied on
    msg.set_content(body or "(empty response)")

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.send_message(msg)
    log.info("replied to %s (tag=%s, session=%s)", msg["To"], tag, (session_id or "")[:8])

    # Map our outbound Message-ID → session so that user replies auto-continue.
    if session_id:
        remember_thread(threads, new_message_id, session_id)


def _footer(lines: list[str]) -> str:
    return "\n\n─\n" + "\n".join(lines)


# ─── handlers ─────────────────────────────────────────────────────────────

def handle_fresh(cfg, threads, msg, bot, prompt, bot_dirs) -> None:
    cwd = bot_dirs[bot]
    if not cwd.is_dir():
        send_reply(cfg, threads, msg, f"error: working directory {cwd} does not exist", bot)
        return
    if not prompt:
        send_reply(cfg, threads, msg, "error: empty prompt", bot)
        return
    try:
        response, session_id = invoke_fresh(cwd, prompt, cfg["claude_bin"], cfg["claude_timeout"])
    except subprocess.TimeoutExpired:
        send_reply(cfg, threads, msg, f"error: claude timed out after {cfg['claude_timeout']}s", bot)
        return
    except Exception as e:
        log.exception("fresh invocation failed")
        send_reply(cfg, threads, msg, f"error: {e}", bot)
        return
    body = response + _footer([
        f"[{bot} · new thread {session_id[:8]}]",
        "Reply to this email to continue the conversation.",
    ])
    send_reply(cfg, threads, msg, body, bot, session_id=session_id)


def handle_resume_session(cfg, threads, msg, session_id: str, prompt: str, *, header: str) -> None:
    """Resume a known session by UUID (no enumeration needed)."""
    if not prompt:
        send_reply(cfg, threads, msg, f"error: empty prompt for thread {session_id[:8]}", "resume")
        return
    # Look up the original cwd by scanning sessions (could cache, but n is small)
    pool = sessions.all_sessions()
    s = next((s for s in pool if s.uuid == session_id), None)
    if not s:
        send_reply(cfg, threads, msg, f"error: session {session_id[:8]} not found in ~/.claude/projects/", "resume")
        return
    if not s.cwd or not pathlib.Path(s.cwd).is_dir():
        send_reply(cfg, threads, msg, f"error: original cwd {s.cwd!r} no longer exists for {session_id[:8]}", "resume")
        return
    cwd = pathlib.Path(s.cwd)
    try:
        response, _ = invoke_resume(cwd, prompt, session_id, cfg["claude_bin"], cfg["claude_timeout"])
    except subprocess.TimeoutExpired:
        send_reply(cfg, threads, msg, f"error: claude timed out after {cfg['claude_timeout']}s", "resume")
        return
    except Exception as e:
        log.exception("resume invocation failed")
        send_reply(cfg, threads, msg, f"error: {e}", "resume")
        return
    body = response + _footer([header, f"  title: {s.title[:120]}"])
    send_reply(cfg, threads, msg, body, "resume", session_id=session_id)


def handle_resume_query(cfg, threads, msg, query, prompt) -> None:
    pool = sessions.all_sessions()
    matches, kind = sessions.find_by_query(query, pool)
    if len(matches) == 0:
        send_reply(cfg, threads, msg, f"no thread matched {query!r}.\n\nUse [list] to see available threads.", "no-match")
        return
    if len(matches) > 1:
        body = (f"{len(matches)} threads matched {query!r} — be more specific.\n\n"
                + sessions.format_list(matches, limit=15, header="matches:"))
        send_reply(cfg, threads, msg, body, "ambiguous")
        return
    s = matches[0]
    handle_resume_session(cfg, threads, msg, s.uuid, prompt,
                          header=f"[resumed {s.uuid[:8]} · {s.cwd} · matched on {kind}]")


def handle_resume_last(cfg, threads, msg, bot, prompt, bot_dirs) -> None:
    if bot not in bot_dirs:
        send_reply(cfg, threads, msg, f"unknown bot {bot!r}", "resume")
        return
    pool = sessions.sessions_for_dir(bot_dirs[bot])
    if not pool:
        send_reply(cfg, threads, msg, f"no prior threads found for {bot}. Use [{bot}] to start fresh.", "resume")
        return
    s = pool[0]  # most recent (sessions.all_sessions sorts by mtime desc)
    handle_resume_session(cfg, threads, msg, s.uuid, prompt,
                          header=f"[resumed {s.uuid[:8]} · most recent in {bot}]")


def handle_list(cfg, threads, msg, bot, bot_dirs) -> None:
    if bot is None:
        pool = sessions.all_sessions()
        header = f"recent threads (across all project dirs · {len(pool)} total):"
    elif bot not in bot_dirs:
        send_reply(cfg, threads, msg, f"unknown bot key {bot!r}. known: {sorted(bot_dirs)}", "list")
        return
    else:
        pool = sessions.sessions_for_dir(bot_dirs[bot])
        header = f"recent threads in {bot_dirs[bot].name} ({len(pool)} total):"
    body = sessions.format_list(pool, limit=20, header=header)
    send_reply(cfg, threads, msg, body, "list")


# ─── dispatch ─────────────────────────────────────────────────────────────

def process_message(cfg, threads, bot_dirs, msg) -> None:
    if msg.get("X-Email-Relay"):
        log.debug("skip: own outbound")
        return
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender not in cfg["authorized_from"]:
        log.info("skip: unauthorized sender %r", sender)
        return

    subject = msg.get("Subject", "")
    parsed = parse_subject(subject, set(bot_dirs.keys()))
    in_reply_session = session_from_in_reply_to(threads, msg)

    log.info("subject=%r action=%s in_reply_to_session=%s",
             subject, parsed["action"], (in_reply_session or "")[:8])

    # Build the prompt: subject remainder (text after [tag]) + body, with quoting stripped.
    raw_body = extract_body(msg)
    clean_body = strip_quoted(raw_body)
    remainder = subject_remainder(subject)
    full_prompt = "\n\n".join(p for p in [remainder, clean_body] if p).strip()

    # Priority dispatch
    action = parsed["action"]
    if action == "skip":
        # No tag in subject — but if it's a reply to a known session, still continue.
        if in_reply_session:
            handle_resume_session(cfg, threads, msg, in_reply_session, full_prompt,
                                  header=f"[continued {in_reply_session[:8]} · via In-Reply-To]")
        return

    if action == "list":
        handle_list(cfg, threads, msg, parsed["bot"], bot_dirs)
        return

    if action == "fresh" and parsed.get("force"):
        # [bot:new] explicitly forces fresh, ignoring In-Reply-To.
        handle_fresh(cfg, threads, msg, parsed["bot"], full_prompt, bot_dirs)
        return

    if action == "resume":
        # Explicit [<query>] — overrides In-Reply-To.
        handle_resume_query(cfg, threads, msg, parsed["query"], full_prompt)
        return

    if action == "resume_last":
        handle_resume_last(cfg, threads, msg, parsed["bot"], full_prompt, bot_dirs)
        return

    # action == "fresh", non-forced: prefer In-Reply-To if available.
    if in_reply_session:
        handle_resume_session(cfg, threads, msg, in_reply_session, full_prompt,
                              header=f"[continued {in_reply_session[:8]} · via In-Reply-To]")
        return

    handle_fresh(cfg, threads, msg, parsed["bot"], full_prompt, bot_dirs)


# ─── poll loop ────────────────────────────────────────────────────────────

def poll_once(cfg, threads, bot_dirs, state) -> None:
    M = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
    try:
        M.login(cfg["imap_user"], cfg["imap_pass"])
        M.select(cfg["imap_mailbox"])
        last_uid = state.get("last_uid", 0)
        typ, data = M.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            log.warning("uid search failed: %s", data)
            return
        uids = [u for u in data[0].split() if int(u) > last_uid]
        if not uids:
            return
        log.info("found %d new message(s)", len(uids))
        for uid in uids:
            typ, msg_data = M.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                log.warning("fetch failed for uid %s", uid)
                state["last_uid"] = max(state["last_uid"], int(uid))
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            try:
                process_message(cfg, threads, bot_dirs, msg)
            except Exception:
                log.exception("processing uid %s failed", uid)
            state["last_uid"] = max(state["last_uid"], int(uid))
            save_state(state)
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_env()
    bot_dirs = load_bot_dirs()
    state = load_state()
    threads = load_threads()
    log.info("relay starting: %d bots, %d known threads, polling every %ds",
             len(bot_dirs), len(threads), cfg["poll_interval"])
    log.info("authorized senders: %s", sorted(cfg["authorized_from"]))

    while True:
        try:
            poll_once(cfg, threads, bot_dirs, state)
        except Exception:
            log.exception("poll cycle failed")
        time.sleep(cfg["poll_interval"])


if __name__ == "__main__":
    main()

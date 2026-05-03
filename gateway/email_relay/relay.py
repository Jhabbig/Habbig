#!/usr/bin/env python3
"""
Email relay: poll Gmail via IMAP, route subject-tagged emails to Claude Code
sessions, reply in-thread with Claude's output.

Subject grammar:
    [<bot_key>] <prompt>     fresh session in that bot's working dir
    [list]                   list recent threads across all your project dirs
    [list:<bot_key>]         list threads for one bot's working dir
    [<query>] <prompt>       resume an existing session by:
                                 UUID prefix (≥6 hex chars), or
                                 case-insensitive substring of the title

Auth:
    From must match AUTHORIZED_FROM (else dropped).
    X-Email-Relay header must be absent (loop guard for our own outbound).

Reply:
    Sent in-thread (In-Reply-To + References), subject "Re: [...]",
    header X-Email-Relay: claude-reply so it can't trigger another cycle.

State:
    Last IMAP UID seen is persisted to state.json so restarts don't reprocess.

Stdlib only. Run with: python3 relay.py
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
BOT_DIRS_FILE = HERE / "bot_dirs.json"
SUBJECT_RE = re.compile(r"\[([^\]]+)\]\s*(.*)", re.DOTALL)

log = logging.getLogger("email_relay")


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


def parse_subject(subject: str, bot_keys: set[str]) -> dict:
    """
    Returns one of:
        {"action": "skip",   "reason": str}
        {"action": "fresh",  "bot": str, "prompt": str}
        {"action": "list",   "bot": str | None}
        {"action": "resume", "query": str, "prompt": str}
    """
    cleaned = re.sub(r"^((re|fwd|fw):\s*)+", "", subject, flags=re.IGNORECASE)
    m = SUBJECT_RE.match(cleaned.strip())
    if not m:
        return {"action": "skip", "reason": "no [tag] in subject"}
    tag = m.group(1).strip()
    rest = m.group(2).strip()

    tag_lower = tag.lower()
    if tag_lower == "list":
        return {"action": "list", "bot": None}
    if tag_lower.startswith("list:"):
        return {"action": "list", "bot": tag_lower.split(":", 1)[1].strip()}

    if tag_lower in bot_keys:
        return {"action": "fresh", "bot": tag_lower, "prompt": rest}

    return {"action": "resume", "query": tag, "prompt": rest}


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


def send_reply(cfg: dict, orig: email.message.Message, body: str, tag: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg["smtp_user"]
    msg["To"] = orig.get("Reply-To") or orig.get("From")
    orig_subject = orig.get("Subject", "")
    msg["Subject"] = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    if orig.get("Message-ID"):
        msg["In-Reply-To"] = orig["Message-ID"]
        msg["References"] = (orig.get("References", "") + " " + orig["Message-ID"]).strip()
    msg["Message-ID"] = make_msgid(domain=cfg["smtp_user"].split("@")[-1])
    msg["X-Email-Relay"] = "claude-reply"
    msg["X-Bot-Key"] = tag
    msg.set_content(body or "(empty response)")

    with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_pass"])
        s.send_message(msg)
    log.info("replied to %s (tag=%s)", msg["To"], tag)


def _footer(lines: list[str]) -> str:
    return "\n\n─\n" + "\n".join(lines)


def handle_fresh(cfg: dict, msg, bot: str, prompt: str, bot_dirs: dict[str, pathlib.Path]) -> None:
    cwd = bot_dirs[bot]
    if not cwd.is_dir():
        send_reply(cfg, msg, f"error: working directory {cwd} does not exist", bot)
        return
    if not prompt:
        send_reply(cfg, msg, "error: empty prompt", bot)
        return
    try:
        response, session_id = invoke_fresh(cwd, prompt, cfg["claude_bin"], cfg["claude_timeout"])
    except subprocess.TimeoutExpired:
        send_reply(cfg, msg, f"error: claude timed out after {cfg['claude_timeout']}s", bot)
        return
    except Exception as e:
        log.exception("fresh invocation failed")
        send_reply(cfg, msg, f"error: {e}", bot)
        return
    body = response + _footer([f"[{bot} · new thread {session_id[:8]}]",
                                "Reply with [" + session_id[:8] + "] <prompt> to continue."])
    send_reply(cfg, msg, body, bot)


def handle_resume(cfg: dict, msg, query: str, prompt: str, bot_dirs: dict[str, pathlib.Path]) -> None:
    pool = sessions.all_sessions()
    matches, kind = sessions.find_by_query(query, pool)

    if len(matches) == 0:
        body = f"no thread matched {query!r}.\n\nUse [list] to see available threads."
        send_reply(cfg, msg, body, "no-match")
        return
    if len(matches) > 1:
        body = (f"{len(matches)} threads matched {query!r} — be more specific.\n\n"
                + sessions.format_list(matches, limit=15, header="matches:"))
        send_reply(cfg, msg, body, "ambiguous")
        return

    s = matches[0]
    if not prompt:
        send_reply(cfg, msg, f"error: empty prompt (matched {s.uuid[:8]} {s.title!r})", "resume")
        return
    if not s.cwd or not pathlib.Path(s.cwd).is_dir():
        send_reply(cfg, msg, f"error: original cwd {s.cwd!r} no longer exists for thread {s.uuid[:8]}", "resume")
        return

    cwd = pathlib.Path(s.cwd)
    try:
        response, _ = invoke_resume(cwd, prompt, s.uuid, cfg["claude_bin"], cfg["claude_timeout"])
    except subprocess.TimeoutExpired:
        send_reply(cfg, msg, f"error: claude timed out after {cfg['claude_timeout']}s", "resume")
        return
    except Exception as e:
        log.exception("resume invocation failed")
        send_reply(cfg, msg, f"error: {e}", "resume")
        return

    body = response + _footer([
        f"[resumed {s.uuid[:8]} · {cwd} · matched on {kind}]",
        f"  title: {s.title}",
    ])
    send_reply(cfg, msg, body, "resume")


def handle_list(cfg: dict, msg, bot: str | None, bot_dirs: dict[str, pathlib.Path]) -> None:
    if bot is None:
        pool = sessions.all_sessions()
        header = f"recent threads (across all project dirs · {len(pool)} total):"
    elif bot not in bot_dirs:
        send_reply(cfg, msg, f"unknown bot key {bot!r}. known: {sorted(bot_dirs)}", "list")
        return
    else:
        pool = sessions.sessions_for_dir(bot_dirs[bot])
        header = f"recent threads in {bot_dirs[bot].name} ({len(pool)} total):"
    body = sessions.format_list(pool, limit=20, header=header)
    send_reply(cfg, msg, body, "list")


def process_message(cfg: dict, bot_dirs: dict[str, pathlib.Path], msg: email.message.Message) -> None:
    if msg.get("X-Email-Relay"):
        log.debug("skip: own outbound")
        return
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if sender not in cfg["authorized_from"]:
        log.info("skip: unauthorized sender %r", sender)
        return

    subject = msg.get("Subject", "")
    parsed = parse_subject(subject, set(bot_dirs.keys()))
    log.info("subject=%r → %s", subject, parsed["action"])

    if parsed["action"] == "skip":
        return
    if parsed["action"] == "list":
        handle_list(cfg, msg, parsed["bot"], bot_dirs)
        return

    body = extract_body(msg)
    full_prompt = (parsed.get("prompt", "") + ("\n\n" + body if body else "")).strip()

    if parsed["action"] == "fresh":
        handle_fresh(cfg, msg, parsed["bot"], full_prompt, bot_dirs)
    elif parsed["action"] == "resume":
        handle_resume(cfg, msg, parsed["query"], full_prompt, bot_dirs)


def poll_once(cfg: dict, bot_dirs: dict[str, pathlib.Path], state: dict) -> None:
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
                process_message(cfg, bot_dirs, msg)
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
    log.info("relay starting: %d bots, polling every %ds", len(bot_dirs), cfg["poll_interval"])
    log.info("authorized senders: %s", sorted(cfg["authorized_from"]))

    while True:
        try:
            poll_once(cfg, bot_dirs, state)
        except Exception:
            log.exception("poll cycle failed")
        time.sleep(cfg["poll_interval"])


if __name__ == "__main__":
    main()

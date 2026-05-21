"""
Read-side helpers for Claude Code's session storage.

Sessions live at ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl. Each line is
a JSON event; the first user-typed message stores `cwd` (the original working
directory) and the prompt text we use as a human-readable "title".

This module never invokes claude — it only inspects on-disk session files.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass

PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"
UUID_LEN = 36           # full UUID
MIN_PREFIX = 6          # shortest UUID prefix we'll accept as a query


@dataclass
class Session:
    uuid: str
    title: str            # first user message, truncated
    cwd: str | None       # original working directory
    project_dir: pathlib.Path
    last_modified: float  # unix ts
    msg_count: int

    def age_str(self) -> str:
        delta = time.time() - self.last_modified
        if delta < 3600:
            return f"{int(delta // 60)}m ago"
        if delta < 86400:
            return f"{int(delta // 3600)}h ago"
        return f"{int(delta // 86400)}d ago"


def _read_session(jsonl: pathlib.Path) -> Session | None:
    title = ""
    cwd = None
    msg_count = 0
    try:
        with jsonl.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t in ("user", "assistant"):
                    msg_count += 1
                if not cwd and ev.get("cwd"):
                    cwd = ev["cwd"]
                if not title and t == "user":
                    msg = ev.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        title = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                title = c.get("text", "")
                                break
                            if isinstance(c, str):
                                title = c
                                break
    except OSError:
        return None
    if not msg_count:
        return None
    return Session(
        uuid=jsonl.stem,
        title=title.strip().replace("\n", " ")[:120],
        cwd=cwd,
        project_dir=jsonl.parent,
        last_modified=jsonl.stat().st_mtime,
        msg_count=msg_count,
    )


def all_sessions() -> list[Session]:
    """Return every session under ~/.claude/projects/, sorted recent first."""
    if not PROJECTS_DIR.is_dir():
        return []
    out: list[Session] = []
    for jsonl in PROJECTS_DIR.glob("*/*.jsonl"):
        s = _read_session(jsonl)
        if s:
            out.append(s)
    out.sort(key=lambda s: s.last_modified, reverse=True)
    return out


def sessions_for_dir(target_dir: pathlib.Path) -> list[Session]:
    """Sessions whose original cwd equals (or is under) target_dir."""
    target = target_dir.resolve()
    out = []
    for s in all_sessions():
        if not s.cwd:
            continue
        try:
            session_cwd = pathlib.Path(s.cwd).resolve()
        except Exception:
            continue
        if session_cwd == target or target in session_cwd.parents:
            out.append(s)
    return out


def find_by_query(query: str, scope: list[Session] | None = None) -> tuple[list[Session], str]:
    """
    Resolve a free-form query to one session.

    Returns (matches, kind):
        matches: candidate sessions (length 0, 1, or >1)
        kind:    "uuid", "title-substr", or "none"
    """
    pool = scope if scope is not None else all_sessions()
    q = query.strip().lower()
    if not q:
        return [], "none"

    # 1. exact UUID
    for s in pool:
        if s.uuid == q:
            return [s], "uuid"
    # 2. UUID prefix (must be hex, ≥MIN_PREFIX chars)
    if len(q) >= MIN_PREFIX and all(c in "0123456789abcdef-" for c in q):
        prefix_matches = [s for s in pool if s.uuid.startswith(q)]
        if prefix_matches:
            return prefix_matches, "uuid"
    # 3. case-insensitive substring of title
    title_matches = [s for s in pool if q in s.title.lower()]
    return title_matches, ("title-substr" if title_matches else "none")


def format_list(sessions: list[Session], limit: int = 20, header: str = "") -> str:
    if not sessions:
        return (header + "\n\n" if header else "") + "no sessions found."
    lines = []
    if header:
        lines.append(header)
        lines.append("")
    shown = sessions[:limit]
    for s in shown:
        cwd_short = pathlib.Path(s.cwd).name if s.cwd else "(unknown)"
        lines.append(f"  {s.uuid[:8]}  {cwd_short:30s}  {s.age_str():>8s}  {s.msg_count:>4d} msgs")
        if s.title:
            lines.append(f"            {s.title}")
    if len(sessions) > limit:
        lines.append(f"\n  … and {len(sessions) - limit} more (showing {limit} most recent)")
    lines.append("")
    lines.append("To resume:  reply with subject  [<id-prefix-or-title-substr>] <your message>")
    return "\n".join(lines)

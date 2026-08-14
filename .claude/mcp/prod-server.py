#!/usr/bin/env python3
"""
narve-prod — read-only stdio MCP server for prodops on the Ubuntu box.

Hand-rolled (stdlib only). All tools are read-only — they shell out to ssh
for status / logs / port checks. State-changing operations (restart, edit,
deploy) are deliberately NOT here — those go through the deploy.sh flow or
explicit user-typed ssh.

Default host: julianhabbig@100.69.44.108 (Tailscale). Override with the
NARVE_PROD_HOST env var.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from typing import Any

PROD_HOST = os.environ.get("NARVE_PROD_HOST", "julianhabbig@100.69.44.108")
SSH_OPTS = ["-o", "ConnectTimeout=8", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
SSH_TIMEOUT = 15  # seconds

SERVER_INFO = {"name": "narve-prod", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-03-26"

# Allowed systemd unit prefix — refuse to query units not matching this
ALLOWED_UNIT_RE = "^polymarket-"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "prod_listening_ports",
        "description": "List all TCP ports currently listening on the prod box (via `ss -tlnp`). Useful for verifying every dashboard is up.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prod_systemctl_is_active",
        "description": "Check whether a polymarket-* systemd unit is active. Returns 'active' / 'inactive' / 'failed' / 'unknown'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string", "description": "Unit name, e.g. 'polymarket-gateway' or 'polymarket-crypto'"},
            },
            "required": ["unit"],
        },
    },
    {
        "name": "prod_systemctl_status_all",
        "description": "Quick health snapshot of every polymarket-* systemd unit (active/inactive/failed, since-when).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prod_journalctl_tail",
        "description": "Tail the systemd journal for a polymarket-* unit. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unit": {"type": "string"},
                "lines": {"type": "integer", "default": 30, "description": "Number of lines (default 30, max 200)"},
                "since": {"type": "string", "description": "Optional 'since' arg, e.g. '5 min ago' or '2026-05-04 18:00'"},
            },
            "required": ["unit"],
        },
    },
    {
        "name": "prod_tail_log",
        "description": "Tail a file under ~/Polymarket/ on the prod box. Read-only. Path must be inside ~/Polymarket/ — refuses anything else.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to ~/Polymarket/ or absolute under it"},
                "lines": {"type": "integer", "default": 30},
            },
            "required": ["path"],
        },
    },
    {
        "name": "prod_curl_local",
        "description": "From the prod box, curl a local-only endpoint (e.g. http://localhost:7000/healthz). Used to verify dashboards behind the gateway from inside the network. URL host must be localhost or 127.0.0.1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "headers": {"type": "object", "description": "Optional request headers", "additionalProperties": {"type": "string"}},
            },
            "required": ["url"],
        },
    },
    {
        "name": "prod_uptime",
        "description": "Get the prod box uptime + load average + free memory.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prod_disk_free",
        "description": "df -h for the prod box's main filesystems.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── SSH layer ──────────────────────────────────────────────────────────────────


async def _ssh(remote_cmd: str) -> tuple[int, str, str]:
    """Run a command on the prod box and return (returncode, stdout, stderr)."""
    cmd = ["ssh", *SSH_OPTS, PROD_HOST, remote_cmd]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SSH_TIMEOUT)
        return proc.returncode or 0, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return 124, "", f"ssh to {PROD_HOST} timed out after {SSH_TIMEOUT}s"


def _validate_unit(unit: str) -> str | None:
    """Return None if unit is allowed, error string if not."""
    import re

    if not re.match(ALLOWED_UNIT_RE, unit):
        return f"Unit '{unit}' rejected — only polymarket-* units allowed"
    if not re.match(r"^[a-zA-Z0-9._@-]+$", unit):
        return f"Unit '{unit}' contains invalid chars"
    return None


def _validate_path(path: str) -> str | None:
    """Path must resolve under ~/Polymarket/."""
    if path.startswith("~/Polymarket/") or path.startswith("/home/julianhabbig/Polymarket/"):
        return None
    if path.startswith("/"):
        return f"Absolute path '{path}' rejected — must be under ~/Polymarket/"
    # Relative path — assume relative to ~/Polymarket/
    return None


def _validate_url(url: str) -> str | None:
    import re

    m = re.match(r"^https?://([^:/]+)(:\d+)?", url)
    if not m:
        return f"URL '{url}' is malformed"
    host = m.group(1)
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return f"Host '{host}' rejected — only localhost / 127.0.0.1 allowed"
    return None


async def call_tool(name: str, args: dict[str, Any]) -> str:
    if name == "prod_listening_ports":
        rc, out, err = await _ssh("ss -tlnp 2>/dev/null | head -60")
        return out if rc == 0 else f"error: {err}"

    if name == "prod_systemctl_is_active":
        unit = args["unit"]
        if e := _validate_unit(unit):
            return e
        rc, out, _ = await _ssh(f"systemctl is-active {shlex.quote(unit)}")
        return out.strip() or "(empty)"

    if name == "prod_systemctl_status_all":
        cmd = (
            "for svc in polymarket-gateway polymarket-crypto polymarket-stock "
            "polymarket-midterm polymarket-traders polymarket-weather "
            "polymarket-sports polymarket-world polymarket-voters "
            "polymarket-climate polymarket-health polymarket-cb; do "
            'state=$(systemctl is-active "$svc" 2>/dev/null); '
            'since=$(systemctl show "$svc" -p ActiveEnterTimestamp --value 2>/dev/null); '
            'printf "  %-22s %-10s (since %s)\\n" "$svc" "$state" "$since"; '
            "done"
        )
        _, out, err = await _ssh(cmd)
        return out or err

    if name == "prod_journalctl_tail":
        unit = args["unit"]
        if e := _validate_unit(unit):
            return e
        lines = min(int(args.get("lines", 30)), 200)
        parts = [f"journalctl -u {shlex.quote(unit)} --no-pager -n {lines}"]
        if since := args.get("since"):
            parts.append(f"--since {shlex.quote(since)}")
        _, out, err = await _ssh(" ".join(parts) + " 2>&1")
        return out or err

    if name == "prod_tail_log":
        path = args["path"]
        if e := _validate_path(path):
            return e
        lines = min(int(args.get("lines", 30)), 500)
        # Resolve to absolute path under ~/Polymarket/
        if not path.startswith("/") and not path.startswith("~"):
            path = f"~/Polymarket/{path}"
        _, out, err = await _ssh(f"tail -n {lines} {shlex.quote(path)} 2>&1")
        return out or err

    if name == "prod_curl_local":
        url = args["url"]
        if e := _validate_url(url):
            return e
        header_args = ""
        for k, v in (args.get("headers") or {}).items():
            header_args += f" -H {shlex.quote(f'{k}: {v}')}"
        cmd = f"curl -sS --max-time 8 -w '\\n[HTTP %{{http_code}}, %{{time_total}}s, %{{size_download}} bytes]\\n'{header_args} {shlex.quote(url)} 2>&1"
        _, out, err = await _ssh(cmd)
        return out or err

    if name == "prod_uptime":
        _, out, err = await _ssh("uptime; echo '---'; free -h")
        return out or err

    if name == "prod_disk_free":
        _, out, err = await _ssh("df -h | head -10")
        return out or err

    raise ValueError(f"unknown tool: {name}")


# ── JSON-RPC framing ───────────────────────────────────────────────────────────


def write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def respond(req_id: Any, *, result: Any = None, error: dict | None = None) -> None:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    write(msg)


def log(msg: str) -> None:
    sys.stderr.write(f"[narve-prod] {msg}\n")
    sys.stderr.flush()


async def handle(req: dict[str, Any]) -> None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        respond(
            rid,
            result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        )
    elif method == "notifications/initialized":
        return
    elif method == "tools/list":
        respond(rid, result={"tools": TOOLS})
    elif method == "tools/call":
        try:
            tool_name = params["name"]
            tool_args = params.get("arguments", {}) or {}
            text = await call_tool(tool_name, tool_args)
            if len(text) > 60_000:
                text = text[:60_000] + "\n\n... [truncated]"
            respond(rid, result={"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            respond(
                rid,
                result={
                    "content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}],
                    "isError": True,
                },
            )
    elif method == "ping":
        respond(rid, result={})
    elif rid is not None:
        respond(rid, error={"code": -32601, "message": f"method not found: {method}"})


async def main() -> None:
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    log(f"started, prod host = {PROD_HOST}")
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            await handle(req)
        except json.JSONDecodeError as e:
            log(f"JSON decode error: {e}")
        except Exception as e:
            log(f"handler crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

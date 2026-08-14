#!/usr/bin/env python3
"""
narve-markets — minimal stdio MCP server exposing Polymarket Gamma + CLOB and
Kalshi public APIs as Claude Code tools.

Hand-rolled (no `mcp` SDK dep). Reads newline-delimited JSON-RPC from stdin,
writes responses to stdout, logs to stderr. Uses only stdlib + httpx (already
installed in the project venv).

All tools are read-only / unauthenticated. No keys required.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx

SERVER_INFO = {"name": "narve-markets", "version": "0.1.0"}
PROTOCOL_VERSION = "2025-03-26"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

TIMEOUT = 12

TOOLS: list[dict[str, Any]] = [
    {
        "name": "polymarket_search_markets",
        "description": "Search active Polymarket markets by keyword via the Gamma API. Returns a list of markets with question, slug, outcomes, prices, and volume.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to search for"},
                "limit": {"type": "integer", "description": "Max results (default 10, max 50)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "polymarket_get_market_by_slug",
        "description": "Fetch a single Polymarket market by its slug. Returns full market detail including outcome token IDs needed for price queries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "The market's slug, e.g. 'will-trump-win-2024'"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "polymarket_get_midpoint",
        "description": "Get the current midpoint price (between best bid/ask) for a Polymarket CLOB token. Token IDs come from polymarket_get_market_by_slug.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_id": {"type": "string", "description": "The CLOB token ID for the YES or NO outcome"},
            },
            "required": ["token_id"],
        },
    },
    {
        "name": "polymarket_get_orderbook",
        "description": "Get the live orderbook for a Polymarket CLOB token — bids and asks with sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token_id": {"type": "string"},
            },
            "required": ["token_id"],
        },
    },
    {
        "name": "kalshi_search_markets",
        "description": "Search active Kalshi event markets by title/subtitle keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to filter by"},
                "limit": {"type": "integer", "description": "Max results (default 20, max 100)", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "kalshi_get_event",
        "description": "Fetch a Kalshi event by its ticker. Returns event metadata and child markets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "event_ticker": {"type": "string", "description": "The event ticker, e.g. 'KXBTC'"},
            },
            "required": ["event_ticker"],
        },
    },
]


# ── HTTP layer ─────────────────────────────────────────────────────────────────


async def _get(client: httpx.AsyncClient, base: str, path: str, params: dict | None = None) -> Any:
    r = await client.get(f"{base}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        if name == "polymarket_search_markets":
            limit = min(int(args.get("limit", 10)), 50)
            return await _get(c, GAMMA, "/markets", {"q": args["query"], "limit": limit, "active": "true", "closed": "false"})

        if name == "polymarket_get_market_by_slug":
            data = await _get(c, GAMMA, "/markets", {"slug": args["slug"]})
            if isinstance(data, list):
                return data[0] if data else {"error": "not found"}
            return data

        if name == "polymarket_get_midpoint":
            return await _get(c, CLOB, "/midpoint", {"token_id": args["token_id"]})

        if name == "polymarket_get_orderbook":
            return await _get(c, CLOB, "/book", {"token_id": args["token_id"]})

        if name == "kalshi_search_markets":
            limit = min(int(args.get("limit", 20)), 100)
            data = await _get(c, KALSHI, "/markets", {"limit": 100, "status": "open"})
            markets = data.get("markets", []) if isinstance(data, dict) else []
            q = args["query"].lower()
            filtered = [m for m in markets if q in (m.get("title", "") + " " + m.get("subtitle", "")).lower()]
            return {"markets": filtered[:limit], "match_count": len(filtered)}

        if name == "kalshi_get_event":
            return await _get(c, KALSHI, f"/events/{args['event_ticker']}")

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
    sys.stderr.write(f"[narve-markets] {msg}\n")
    sys.stderr.flush()


# ── Dispatcher ─────────────────────────────────────────────────────────────────


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
        return  # notification — no response
    elif method == "tools/list":
        respond(rid, result={"tools": TOOLS})
    elif method == "tools/call":
        try:
            tool_name = params["name"]
            tool_args = params.get("arguments", {}) or {}
            result = await call_tool(tool_name, tool_args)
            text = json.dumps(result, indent=2, default=str)
            # Truncate very large payloads — model context isn't unlimited
            if len(text) > 60_000:
                text = text[:60_000] + "\n\n... [truncated, response was too large]"
            respond(
                rid,
                result={
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            )
        except httpx.HTTPStatusError as e:
            respond(
                rid,
                result={
                    "content": [{"type": "text", "text": f"HTTP {e.response.status_code}: {e.response.text[:500]}"}],
                    "isError": True,
                },
            )
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

    log("started")
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"JSON decode error: {e}")
            continue
        try:
            await handle(req)
        except Exception as e:
            log(f"handler crashed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

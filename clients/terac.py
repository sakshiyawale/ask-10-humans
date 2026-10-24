"""Minimal client for the Terac MCP server (streamable HTTP, JSON-RPC 2.0).

Terac exposes its API as MCP tools (terac_create_opportunity, terac_launch_draft_opportunity, ...).
We speak the protocol directly with httpx, so no MCP SDK is needed.
"""
import itertools
import json
import logging

import httpx

import config

log = logging.getLogger("terac")


class TeracError(RuntimeError):
    pass


class TeracMCP:
    def __init__(self, api_key: str | None = None, url: str | None = None):
        self.api_key = api_key or config.TERAC_API_KEY
        self.url = url or config.TERAC_MCP_URL
        self.session_id: str | None = None
        self._ids = itertools.count(1)

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _post(self, payload: dict) -> dict | None:
        r = httpx.post(self.url, headers=self._headers(), json=payload, timeout=90)
        if r.status_code >= 400:
            raise TeracError(f"Terac HTTP {r.status_code}: {r.text[:300]}")
        if "mcp-session-id" in r.headers:
            self.session_id = r.headers["mcp-session-id"]
        if "id" not in payload:  # notification, no body expected
            return None
        return _parse_response(r.text, payload["id"])

    def _initialize(self) -> None:
        self.session_id = None
        self._post({
            "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "ask-10-humans", "version": "1.0"}},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        """Call a Terac tool and return its structured result."""
        if not self.api_key:
            raise TeracError("TERAC_API_KEY is not set")
        for attempt in (1, 2):
            if not self.session_id:
                self._initialize()
            try:
                msg = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                                  "params": {"name": tool, "arguments": arguments or {}}})
                break
            except TeracError:
                if attempt == 2:
                    raise
                self.session_id = None  # session may have expired; retry once
        if "error" in msg:
            raise TeracError(f"{tool}: {msg['error']}")
        result = msg.get("result", {})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise TeracError(f"{tool}: {text[:500]}")
        if isinstance(result.get("structuredContent"), dict):
            return result["structuredContent"]
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return {"text": text}


def _parse_response(body: str, want_id) -> dict:
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    # Server-sent events: pick the "data:" line carrying our response id.
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                msg = json.loads(line[5:].strip())
            except ValueError:
                continue
            if msg.get("id") == want_id:
                return msg
    raise TeracError(f"No response for request {want_id}: {body[:300]}")


def find(obj, key: str):
    """Depth-first search for the first value under `key` in nested dicts/lists."""
    if isinstance(obj, dict):
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
        for v in obj.values():
            hit = find(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, key)
            if hit is not None:
                return hit
    return None

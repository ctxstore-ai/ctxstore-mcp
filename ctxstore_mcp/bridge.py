"""
Forward MCP tools/list and tools/call to the hosted ctxstore server.

The stdio package used to carry its own copy of a handful of tools, mapped
onto the REST API. The hosted server at https://mcp.ctxstore.ai/mcp has the
full toolset (setup_account, bind_agent, wake_status, workspaces, invites),
and session-scoped tools such as bind_agent only exist there. So the stdio
server now speaks streamable HTTP to the hosted server and passes the tool
list and every call through unchanged, holding one remote session for the
life of the process so a binding made by bind_agent sticks.

Plain JSON-RPC over httpx, not the mcp client classes, so the same code runs
on mcp 1.x and 2.x. If the hosted server cannot be reached, server.py falls
back to the local REST tool set.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

import httpx

DEFAULT_MCP_URL = "https://mcp.ctxstore.ai/mcp"
PROTOCOL_VERSION = "2025-06-18"
_KEY_RE = re.compile(r"\bemk_[A-Za-z0-9_-]{16,}")


class RemoteUnavailable(Exception):
    """The hosted server could not be reached or did not speak MCP."""


class RemoteBridge:
    def __init__(
        self,
        key_fn: Callable[[], str],
        url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "ctxstore-mcp",
    ):
        self.url = url or os.getenv("CTXSTORE_MCP_URL", DEFAULT_MCP_URL)
        self._key_fn = key_fn
        self._transport = transport
        self._user_agent = user_agent
        self._initialized = False
        self._next_id = 0
        self.session_id: str | None = None
        self.key_override: str | None = None
        self.instructions: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self._user_agent,
        }
        key = self.key_override or self._key_fn()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _post(self, payload: dict) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as client:
                return await client.post(self.url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise RemoteUnavailable(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _parse(resp: httpx.Response, req_id: int) -> dict:
        try:
            if "text/event-stream" in resp.headers.get("content-type", ""):
                for line in resp.text.splitlines():
                    if line.startswith("data:") and line[5:].strip():
                        msg = json.loads(line[5:])
                        if msg.get("id") == req_id:
                            return msg
                raise RemoteUnavailable("no response in the event stream")
            return resp.json()
        except ValueError as exc:
            raise RemoteUnavailable(f"HTTP {resp.status_code}, not JSON-RPC") from exc

    async def _initialize(self) -> None:
        self.session_id = None
        self._next_id += 1
        req_id = self._next_id
        resp = await self._post({
            "jsonrpc": "2.0", "id": req_id, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ctxstore-mcp-stdio", "version": self._user_agent.split("/")[-1]},
            },
        })
        msg = self._parse(resp, req_id)
        if "error" in msg or "result" not in msg:
            raise RemoteUnavailable(f"initialize failed: {msg.get('error')}")
        self.session_id = resp.headers.get("mcp-session-id")
        self.instructions = msg["result"].get("instructions")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def request(self, method: str, params: dict | None = None, _retry: bool = True) -> dict:
        if not self._initialized:
            await self._initialize()
        self._next_id += 1
        req_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._post(payload)
        if resp.status_code in (400, 404) and self.session_id and _retry:
            # The remote session expired or the server restarted: open a new one once.
            self._initialized = False
            return await self.request(method, params, _retry=False)
        return self._parse(resp, req_id)

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            msg = await self.request("tools/list", {"cursor": cursor} if cursor else None)
            if "error" in msg:
                raise RemoteUnavailable(f"tools/list failed: {msg['error']}")
            tools.extend(msg["result"].get("tools", []))
            cursor = msg["result"].get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict) -> tuple[list[dict], bool]:
        """Returns (content items as dicts, is_error)."""
        msg = await self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if "error" in msg:
            err = msg["error"]
            text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return [{"type": "text", "text": text}], True
        result = msg.get("result", {})
        content = result.get("content", [])
        if name == "setup_account" and not result.get("isError"):
            # A verify or restore hands back a key: use it for the rest of this process.
            for item in content:
                found = _KEY_RE.search(item.get("text", "") or "")
                if found:
                    self.key_override = found.group(0)
        return content, bool(result.get("isError"))

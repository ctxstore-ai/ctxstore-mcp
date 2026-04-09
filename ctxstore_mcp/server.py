"""
ctxstore MCP Server — thin client that proxies to ctxstore.ai API.

All embedding generation and vector storage happens server-side.
This package just translates MCP tool calls to HTTP API calls.

Environment variables:
  TENANT_API_KEY  — Your ctxstore.ai API key (required)
  CTXSTORE_URL    — API base URL (default: https://ctxstore.ai)
"""

import logging
import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .auth import resolve_api_key

logger = logging.getLogger("ctxstore.mcp")

BASE_URL = os.getenv("CTXSTORE_URL", "https://ctxstore.ai").rstrip("/")

# Resolved lazily on first use so startup auto-provision prints before the
# server enters the stdio event loop.
_API_KEY: str | None = None


def _get_api_key() -> str:
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = resolve_api_key()
    return _API_KEY

app = Server("ctxstore")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": "ctxstore-mcp/1.0.0",
    }


async def _api(method: str, path: str, json: dict = None) -> dict:
    """Make an authenticated API call to ctxstore.ai."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method,
            f"{BASE_URL}{path}",
            headers=_headers(),
            json=json,
        )
        if resp.status_code == 401:
            return {"error": "Invalid API key. Check your TENANT_API_KEY."}
        if resp.status_code == 429:
            return {"error": "Rate limit reached. Upgrade your plan at ctxstore.ai."}
        if resp.status_code >= 400:
            try:
                return resp.json()
            except Exception:
                return {"error": f"API error: {resp.status_code}"}
        return resp.json()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_context",
            description=(
                "Search memory — all ingested conversations and sessions. "
                "Returns semantically relevant context with temporal weighting "
                "(recent = higher rank)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results (default 20, max 100)",
                        "default": 20,
                    },
                    "source": {
                        "type": "string",
                        "description": "Filter by source: 'chatgpt' or 'claude'",
                        "enum": ["chatgpt", "claude"],
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Only search within last N days",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_facts",
            description=(
                "Search extracted facts, preferences, and decisions. "
                "Facts are tagged by category: preference, decision, identity, "
                "technical, relationship. Permanent facts bypass temporal decay."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category",
                        "enum": ["preference", "decision", "identity", "technical", "relationship"],
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="store_fact",
            description=(
                "Store a new fact for permanent retention. Use this when the user "
                "states a preference, makes a decision, or shares information that "
                "should persist across sessions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The fact to store",
                    },
                    "category": {
                        "type": "string",
                        "description": "Fact category",
                        "enum": ["preference", "decision", "identity", "technical", "relationship"],
                    },
                    "is_permanent": {
                        "type": "boolean",
                        "description": "Whether this fact should bypass temporal decay",
                        "default": True,
                    },
                },
                "required": ["text", "category"],
            },
        ),
        Tool(
            name="delete_fact",
            description="Delete a stored fact by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "fact_id": {
                        "type": "string",
                        "description": "The UUID of the fact to delete",
                    },
                },
                "required": ["fact_id"],
            },
        ),
        Tool(
            name="get_stats",
            description=(
                "Get statistics about your context store — total vectors, "
                "collection sizes, usage vs plan limits."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if not _get_api_key():
        return [TextContent(
            type="text",
            text="No API key configured. Set TENANT_API_KEY in your MCP config. "
                 "Get a free key at https://ctxstore.ai",
        )]

    if name == "search_context":
        result = await _api("POST", "/api/v1/search", {
            "query": arguments["query"],
            "top_k": arguments.get("top_k", 20),
            "source": arguments.get("source"),
            "days_back": arguments.get("days_back"),
        })
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=_format_search_results(result))]

    elif name == "search_facts":
        result = await _api("POST", "/api/v1/facts/search", {
            "query": arguments["query"],
            "category": arguments.get("category"),
            "top_k": arguments.get("top_k", 10),
        })
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=_format_fact_results(result))]

    elif name == "store_fact":
        result = await _api("POST", "/api/v1/facts", {
            "text": arguments["text"],
            "category": arguments["category"],
            "is_permanent": arguments.get("is_permanent", True),
        })
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(
            type="text",
            text=f"Fact stored (id={result.get('fact_id', '?')}, "
                 f"category={arguments['category']}, "
                 f"permanent={arguments.get('is_permanent', True)}): "
                 f"{arguments['text']}",
        )]

    elif name == "delete_fact":
        result = await _api("DELETE", f"/api/v1/facts/{arguments['fact_id']}")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=f"Fact {arguments['fact_id']} deleted.")]

    elif name == "get_stats":
        result = await _api("GET", "/api/v1/stats")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=_format_stats(result))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def _format_search_results(data: dict) -> str:
    results = data.get("results", [])
    if not results:
        return "No relevant context found."
    formatted = []
    for r in results:
        header = f"[{r.get('source', '?')}|{r.get('age', '?')}|score:{r.get('score', 0):.3f}]"
        title = r.get("conversation_title", "")
        if title:
            header += f" ({title})"
        formatted.append(f"{header}\n{r.get('text', '')}\n")
    return "\n---\n".join(formatted)


def _format_fact_results(data: dict) -> str:
    results = data.get("results", [])
    if not results:
        return "No matching facts found."
    formatted = []
    for r in results:
        perm = "permanent" if r.get("is_permanent") else "decaying"
        formatted.append(
            f"[{r.get('category', '?')}|{perm}|score:{r.get('score', 0):.3f}] "
            f"{r.get('text', '')}"
        )
    return "\n".join(formatted)


def _format_stats(data: dict) -> str:
    import json
    return json.dumps(data, indent=2)


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    # Resolve API key on startup (triggers auto-provision if needed)
    api_key = _get_api_key()
    if not api_key:
        logger.warning("No API key resolved — tools will prompt for signup")

    logger.info(f"ctxstore MCP server starting (endpoint: {BASE_URL})")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )

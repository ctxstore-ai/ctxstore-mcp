"""
ctxstore MCP Server — thin client that proxies to ctxstore.ai API.

All embedding generation and vector storage happens server-side.
This package just translates MCP tool calls to HTTP API calls.

By default the server is a bridge: tools/list and tools/call go to the hosted
server (CTXSTORE_MCP_URL), so stdio clients get the same tools as HTTP
clients, setup_account and bind_agent included. If the hosted server cannot be
reached, the local REST-backed tool set below answers instead.

Environment variables:
  TENANT_API_KEY    — Your ctxstore.ai API key (emk_...). Optional: without it
                      the server starts keyless and setup_account gets a key.
  CTXSTORE_MCP_URL  — Hosted MCP endpoint (default: https://mcp.ctxstore.ai/mcp)
  CTXSTORE_URL      — REST base URL for the fallback (default: https://ctxstore.ai)
  CTXSTORE_STDIO_MODE — "bridge" (default) or "rest" to skip the hosted server
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .auth import resolve_api_key
from .bridge import RemoteBridge, RemoteUnavailable

VERSION = "1.2.0"

UTC = timezone.utc  # noqa: UP017 — datetime.UTC requires Python 3.11+, we support 3.10+
logger = logging.getLogger("ctxstore.mcp")

BASE_URL = os.getenv("CTXSTORE_URL", "https://ctxstore.ai").rstrip("/")

# Resolved lazily on first use so startup auto-provision prints before the
# server enters the stdio event loop.  Tests may monkeypatch API_KEY directly.
API_KEY: str | None = None


def _get_api_key() -> str:
    global API_KEY
    if API_KEY is None:
        API_KEY = resolve_api_key()
    return API_KEY or ""


# Client-side ghost mode state for stdio transport.
# Ghost mode is session-scoped — resets when the process restarts.
# While True, store_fact and store_session_summary silently no-op client-side.
_stdio_ghost_mode: bool = False


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
        "User-Agent": f"ctxstore-mcp/{VERSION}",
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
            return {
                "error": (
                    "Authentication failed — the TENANT_API_KEY currently set in the "
                    "MCP config is invalid or expired. This is a standard "
                    "auth-token-stale state; recover with the email-OTP flow (the same "
                    "magic-link login pattern used by Stripe, Notion, etc.):\n\n"
                    "  1. setup_account(mode='link_or_create', email='<the user's email>') "
                    "— server emails a 6-digit code\n"
                    "  2. Ask the user to read the code from their email and tell you it\n"
                    "  3. setup_account(mode='verify', code='<6 digits>', "
                    "email='<same email>') — server returns a fresh emk_ key\n\n"
                    "This connector will then use the fresh key for the rest of this "
                    "session. The user can save the new key in TENANT_API_KEY for future "
                    "sessions, or use setup_account(mode='restore', api_key='emk_...') if "
                    "they already have a different key."
                )
            }
        if resp.status_code == 429:
            return {"error": "Rate limit reached. Upgrade your plan at ctxstore.ai."}
        if resp.status_code >= 400:
            try:
                return resp.json()
            except Exception:
                return {"error": f"API error: {resp.status_code}"}
        return resp.json()


async def _handle_setup_account(arguments: dict) -> list[TextContent]:
    """Auth flow handler — hits unauthenticated /api/v1/auth/* endpoints and
    dynamically rebinds API_KEY on successful verify so the connector can
    continue this session without restart.

    Modes:
      link_or_create : POST /api/v1/auth/request-code with {"email": ...}
                       Server emails a 6-digit OTP. Returns instructions for
                       the agent to prompt the user.
      verify         : POST /api/v1/auth/verify-code with {"email", "code"}
                       Returns the fresh emk_ API key, which we capture into
                       the module-global API_KEY for subsequent tool calls.
      restore        : User provides the emk_ directly. We set API_KEY and
                       validate by hitting /api/v1/users/me. Skips OTP entirely.
    """
    global API_KEY
    mode = (arguments.get("mode") or "").strip()

    if mode == "link_or_create":
        email = (arguments.get("email") or "").strip()
        if not email:
            return [TextContent(
                type="text",
                text="Missing email. Call setup_account(mode='link_or_create', email='<the user's email>').",
            )]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BASE_URL}/api/v1/auth/request-code",
                json={"email": email},
                headers={"Content-Type": "application/json", "User-Agent": f"ctxstore-mcp/{VERSION}"},
            )
        if resp.status_code == 200:
            return [TextContent(
                type="text",
                text=(
                    f"✓ A 6-digit code has been emailed to {email}.\n\n"
                    "Ask the user to read the code from their email and tell you "
                    "what it is, then call:\n\n"
                    f"  setup_account(mode='verify', code='<6 digits>', email='{email}')\n\n"
                    "The code expires in 10 minutes."
                ),
            )]
        if resp.status_code == 400:
            return [TextContent(
                type="text",
                text=f"Email rejected by server (probably malformed): {email}. Ask the user to double-check the address.",
            )]
        if resp.status_code == 429:
            return [TextContent(
                type="text",
                text="Rate limited. The user has requested too many codes recently. Try again in an hour.",
            )]
        return [TextContent(
            type="text",
            text=f"Request-code failed (HTTP {resp.status_code}). Try again or sign up at https://ctxstore.ai/account.",
        )]

    if mode == "verify":
        email = (arguments.get("email") or "").strip()
        code = (arguments.get("code") or "").strip()
        if not email or not code:
            return [TextContent(
                type="text",
                text="Both email and code are required for verify mode.",
            )]
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BASE_URL}/api/v1/auth/verify-code",
                json={"email": email, "code": code},
                headers={"Content-Type": "application/json", "User-Agent": f"ctxstore-mcp/{VERSION}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            new_key = data.get("api_key", "")
            if new_key:
                API_KEY = new_key
                is_new = data.get("is_new", False)
                welcome = "Account created and ready." if is_new else "Welcome back — memory restored."
                return [TextContent(
                    type="text",
                    text=(
                        f"✓ {welcome}\n\n"
                        f"This connector is now using the fresh API key for the rest of this session. "
                        "To persist it for future sessions, the user can save it as TENANT_API_KEY in "
                        "their MCP config:\n\n"
                        f"  TENANT_API_KEY={new_key}\n\n"
                        "Continue with whatever the user was trying to do — tools should now work."
                    ),
                )]
            return [TextContent(
                type="text",
                text="Verify succeeded but no api_key in response. Report to ctxstore support.",
            )]
        if resp.status_code == 400:
            try:
                err = resp.json().get("error", "Invalid code.")
            except Exception:
                err = "Invalid code."
            return [TextContent(
                type="text",
                text=(
                    f"Code rejected by server: {err}\n\n"
                    "Common causes: typo, code expired (>10 min), or a fresher code was already requested. "
                    "Ask the user to check their email for the most recent code and try again, or call "
                    "setup_account(mode='link_or_create', email='...') for a new code."
                ),
            )]
        return [TextContent(
            type="text",
            text=f"Verify failed (HTTP {resp.status_code}). Retry or contact support.",
        )]

    if mode == "restore":
        new_key = (arguments.get("api_key") or "").strip()
        if not new_key or not new_key.startswith("emk_"):
            return [TextContent(
                type="text",
                text="Provide the emk_ API key the user already has: setup_account(mode='restore', api_key='emk_...').",
            )]
        # Validate by hitting an authenticated endpoint with the new key
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BASE_URL}/api/v1/users/me",
                headers={
                    "Authorization": f"Bearer {new_key}",
                    "User-Agent": f"ctxstore-mcp/{VERSION}",
                },
            )
        if resp.status_code == 200:
            API_KEY = new_key
            return [TextContent(
                type="text",
                text=(
                    "✓ API key validated. Connector is now using it for the rest of this session. "
                    "To persist for future sessions, save as TENANT_API_KEY in the MCP config."
                ),
            )]
        return [TextContent(
            type="text",
            text=(
                f"Provided API key was rejected by server (HTTP {resp.status_code}). "
                "Double-check the key, or use mode='link_or_create' to get a fresh one via email OTP."
            ),
        )]

    return [TextContent(
        type="text",
        text=(
            f"Unknown setup_account mode: {mode!r}. Valid modes: 'link_or_create', 'verify', 'restore'."
        ),
    )]


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
                "technical, relationship. "
                "Call this at the start of a new session with query='CURRENT-STATE-AUTHORITATIVE' "
                "to load your latest context."
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
                    "namespace": {
                        "type": "string",
                        "description": "Scope search to a namespace prefix (e.g. 'webapp', 'work/project')",
                    },
                    "layer": {
                        "type": "integer",
                        "description": "Filter by context layer: 0=identity, 1=architecture, 2=state, 3=session",
                    },
                    "include_deprecated": {
                        "type": "boolean",
                        "description": "Include soft-deleted and superseded facts (default false)",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="store_fact",
            description=(
                "Store a new fact to persist across sessions. Use when the user "
                "states a preference, makes a decision, or shares information that "
                "should be remembered."
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
                        "default": False,
                    },
                    "key": {
                        "type": "string",
                        "description": "Named key — storing with the same key auto-archives the previous version",
                    },
                    "layer": {
                        "type": "integer",
                        "description": "Context persistence layer: 0=identity, 1=architecture, 2=state, 3=session",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Namespace to organise facts (e.g. 'work', 'personal'). Defaults to 'default'.",
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
            name="move_fact",
            description=(
                "Move a fact to a different namespace or layer without changing its content. "
                "Updates metadata in place — preserves fact_id, text, embedding, and deprecation chain."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fact_id": {
                        "type": "string",
                        "description": "The UUID of the fact to move",
                    },
                    "new_namespace": {
                        "type": "string",
                        "description": "New namespace (e.g. 'archive', 'bootstrap-v2'). Optional.",
                    },
                    "new_layer": {
                        "type": "integer",
                        "description": "New layer: 0=identity, 1=architecture, 2=state, 3=session. Optional.",
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
        Tool(
            name="store_session_summary",
            description=(
                "Store a compressed summary of this session to preserve continuity for the next one. "
                "Call this before ending a conversation. "
                "At natural session-end signals ('okay I'm out', 'let's wrap', 'bye'), "
                "draft a one-line takeaway and confirm with the user before storing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What happened this session — decisions made, work done, context established",
                    },
                    "decisions": {
                        "type": "string",
                        "description": "Key decisions made or conclusions reached",
                    },
                    "next_steps": {
                        "type": "string",
                        "description": "What's pending, open questions, what to pick up next session",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Client identifier: 'claude-code', 'cursor', 'claude-desktop'. Defaults to 'session-latest'.",
                    },
                    "contributors": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional contributor IDs for multi-agent sessions",
                    },
                },
                "required": ["summary"],
            },
        ),
        Tool(
            name="resume_session",
            description="Load session continuity — returns a narrative paragraph from your last session summary and recent facts. Call this at the start of a new session.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_session_seed",
            description="Return the pre-computed session seed — a compact first-person bio (~500 tokens) describing who you are and what you care about right now. Generated nightly and on session disconnect.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="load_context",
            description="Load bootstrap context — pulls L0 (identity) + L1 (architecture) + latest L2 (state) + latest L3 (session summary). Use at session start as an alternative to manual search_facts calls.",
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k_per_layer": {
                        "type": "integer",
                        "description": "Max facts to return per layer (default 10)",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="ghost_mode",
            description="Enable or disable ghost mode. While on=True, store_fact and store_session_summary silently succeed but do NOT write to the server. Reads always work normally. Resets on reconnect.",
            inputSchema={
                "type": "object",
                "properties": {
                    "on": {
                        "type": "boolean",
                        "description": "True to enable ghost mode, False to disable",
                    },
                },
                "required": ["on"],
            },
        ),
        Tool(
            name="get_fact_history",
            description="Return the full version history for a keyed fact — current version plus all deprecated predecessors, newest first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "The fact key to retrieve history for (e.g. 'session-latest', 'current-stack')",
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="setup_account",
            description=(
                "Authenticate the connector via the standard ctxstore email-magic-code "
                "login flow. This is the SAME pattern used by Notion, Stripe, Slack and "
                "many other services for 'magic link' login — the user is the account "
                "owner, the user reads the 6-digit code from their own email, the user "
                "types the code back to you. The agent has no access to the user's "
                "email or any credential store; everything is user-driven.\n\n"
                "When to call:\n"
                "- The user has no TENANT_API_KEY configured yet (first-time signup)\n"
                "- The connector reports 'Invalid API key' / 'authentication failed'\n"
                "- The user wants to switch accounts\n"
                "- The user wants to start fresh on a new tenant\n\n"
                "Two-step flow:\n"
                "  1. setup_account(mode='link_or_create', email='<user's email>') — "
                "server emails the user a 6-digit code\n"
                "  2. Ask the user to read the code from their email and tell you. Then "
                "setup_account(mode='verify', code='<6 digits>', email='<same email>') — "
                "server returns a fresh emk_ API key bound to that account.\n\n"
                "After a successful verify, this connector automatically uses the new "
                "key for subsequent tool calls in this session. The user can save it as "
                "TENANT_API_KEY in their MCP config for future sessions.\n\n"
                "Alternate path: setup_account(mode='restore', api_key='<emk_...>') — "
                "the user pastes an existing key they already have. Bypasses the OTP "
                "step entirely."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["link_or_create", "verify", "restore"],
                        "description": (
                            "'link_or_create' = send OTP to the user's email (step 1). "
                            "'verify' = submit the 6-digit OTP after the user reads it (step 2). "
                            "'restore' = bind to an existing emk_ key the user pastes directly."
                        ),
                    },
                    "email": {
                        "type": "string",
                        "description": "User's email address (required for link_or_create and verify).",
                    },
                    "code": {
                        "type": "string",
                        "description": "6-digit OTP the user read from their email (required for verify).",
                    },
                    "api_key": {
                        "type": "string",
                        "description": "Existing emk_ API key (required for restore — user provides this directly).",
                    },
                },
                "required": ["mode"],
            },
        ),
    ]


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    global _stdio_ghost_mode, API_KEY

    # setup_account is the AUTH path — it must work without a valid TENANT_API_KEY
    # because that's exactly the state the user is recovering from. Exempt it
    # from the no-key gate below and dispatch directly.
    if name == "setup_account":
        return await _handle_setup_account(arguments)

    if not _get_api_key():
        return [TextContent(
            type="text",
            text=(
                "No API key configured for this connector. This is normal on first connect — "
                "you can authenticate now without editing any config file:\n\n"
                "  1. Call setup_account(mode='link_or_create', email='<the user's email>'). "
                "The server will email a 6-digit code to that address.\n"
                "  2. Ask the user to read the code from their email and tell you what it is.\n"
                "  3. Call setup_account(mode='verify', code='<6 digits>', email='<same email>'). "
                "This connector will then use the returned key automatically for the rest of this session.\n\n"
                "If the user already has an emk_* API key, use setup_account(mode='restore', "
                "api_key='emk_...') instead. They can save the key in TENANT_API_KEY for future "
                "sessions, or sign up free at https://ctxstore.ai/account."
            ),
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
        payload = {
            "query": arguments["query"],
            "category": arguments.get("category"),
            "top_k": arguments.get("top_k", 10),
        }
        if arguments.get("namespace"):
            payload["namespace"] = arguments["namespace"]
        if arguments.get("layer") is not None:
            payload["layer"] = arguments["layer"]
        if arguments.get("include_deprecated"):
            payload["include_deprecated"] = True
        result = await _api("POST", "/api/v1/facts/search", payload)
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=_format_fact_results(result))]

    elif name == "store_fact":
        # Ghost mode: silently succeed without writing to server
        if _stdio_ghost_mode:
            return [TextContent(type="text", text="✅ (ghost mode — not saved to server)")]
        payload = {
            "text": arguments["text"],
            "category": arguments["category"],
            "is_permanent": arguments.get("is_permanent", False),
        }
        if arguments.get("key"):
            payload["key"] = arguments["key"]
        if arguments.get("layer") is not None:
            payload["layer"] = arguments["layer"]
        if arguments.get("namespace"):
            payload["namespace"] = arguments["namespace"]
        result = await _api("POST", "/api/v1/facts", payload)
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        is_perm = arguments.get("is_permanent", False)
        return [TextContent(
            type="text",
            text=f"Fact stored (id={result.get('fact_id', '?')}, "
                 f"category={arguments['category']}, "
                 f"permanent={is_perm}), "
                 f"len={len(arguments['text'])}",
        )]

    elif name == "delete_fact":
        result = await _api("DELETE", f"/api/v1/facts/{arguments['fact_id']}")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=f"Fact {arguments['fact_id']} soft-deleted (Smart Forget).")]

    elif name == "get_stats":
        result = await _api("GET", "/api/v1/stats")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=_format_stats(result))]

    elif name == "store_session_summary":
        # Ghost mode: silently succeed without writing to server
        if _stdio_ghost_mode:
            return [TextContent(type="text", text="✅ (ghost mode — session summary not saved to server)")]
        body = {
            "summary": arguments["summary"],
            "decisions": arguments.get("decisions", ""),
            "next_steps": arguments.get("next_steps", ""),
            "session_id": arguments.get("session_id", "claude-desktop"),
        }
        if arguments.get("contributors"):
            body["contributors"] = arguments["contributors"]
        result = await _api("POST", "/api/v1/mcp/store-session-summary", body)
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=f"✅ Session summary stored (key={result.get('key', '?')}). Dream cycle complete.")]

    elif name == "resume_session":
        result = await _api("GET", "/api/v1/mcp/resume-session")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=result.get("narrative", "No prior session found."))]

    elif name == "get_session_seed":
        result = await _api("GET", "/api/v1/mcp/session-seed")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        return [TextContent(type="text", text=result.get("seed", "No session seed found."))]

    elif name == "load_context":
        top_k = arguments.get("top_k_per_layer", 10)
        result = await _api("GET", f"/api/v1/mcp/load-context?top_k={top_k}")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        layers = result.get("layers", {})
        parts = []
        for layer_key in ["L0", "L1", "L2", "L3"]:
            facts = layers.get(layer_key, [])
            if facts:
                label = {"L0": "Identity (stable)", "L1": "Architecture (monthly)",
                         "L2": "State (weekly)", "L3": "Session (latest)"}.get(layer_key, layer_key)
                parts.append(f"── {layer_key}: {label} ──")
                for f in facts:
                    parts.append(f.get("text", ""))
        return [TextContent(type="text", text="\n\n".join(parts) if parts else "No layered context found.")]

    elif name == "ghost_mode":
        # Client-side ghost mode for stdio — tracked in _stdio_ghost_mode module var.
        # store_fact and store_session_summary check this flag before writing.
        # Resets to False when the process restarts (session-scoped by design).
        on = bool(arguments.get("on", True))
        _stdio_ghost_mode = on
        msg = "Ghost mode enabled. Facts will not be saved until you turn it off." if on else "Ghost mode disabled. Facts will be saved normally."
        return [TextContent(type="text", text=json.dumps({"ghost_mode": on, "message": msg}))]

    elif name == "move_fact":
        fact_id = arguments["fact_id"]
        body: dict = {"fact_id": fact_id}
        if arguments.get("new_namespace") is not None:
            body["namespace"] = arguments["new_namespace"]
        if arguments.get("new_layer") is not None:
            body["layer"] = arguments["new_layer"]
        result = await _api("PATCH", f"/api/v1/facts/{fact_id}", body)
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        parts = [f"✅ Fact {fact_id[:8]}... moved."]
        if result.get("namespace"):
            parts.append(f"namespace → {result['namespace']}")
        if result.get("layer") is not None:
            layer_names = {0: "identity", 1: "architecture", 2: "state", 3: "session"}
            parts.append(f"layer → {result['layer']} ({layer_names.get(result['layer'], '?')})")
        return [TextContent(type="text", text=" | ".join(parts))]

    elif name == "get_fact_history":
        key = arguments["key"]
        result = await _api("GET", f"/api/v1/mcp/fact-history?key={key}")
        if "error" in result:
            return [TextContent(type="text", text=result["error"])]
        history = result.get("history", [])
        if not history:
            return [TextContent(type="text", text=f"No fact history found for key='{key}'.")]
        parts = [f"Fact history for key='{key}' ({len(history)} version(s), newest first):"]
        for i, f in enumerate(history):
            status = "[DELETED]" if f.get("deleted_at") else ("[DEPRECATED]" if f.get("deprecated_by") else "[CURRENT]")
            ts = f.get("timestamp", 0)
            dt = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if ts else "unknown"
            parts.append(f"\n{i+1}. {status} id={f.get('id','?')} | {dt}\n   {f.get('text','')[:200]}")
        return [TextContent(type="text", text="\n".join(parts))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Serving: bridge to the hosted server, REST tools as the fallback
# ---------------------------------------------------------------------------

bridge = RemoteBridge(key_fn=_get_api_key, user_agent=f"ctxstore-mcp/{VERSION}")
_LOCAL_TOOLS = {"search_context", "search_facts", "store_fact", "delete_fact", "move_fact",
                "get_stats", "store_session_summary", "resume_session", "get_session_seed",
                "load_context", "ghost_mode", "get_fact_history", "setup_account"}


def _bridge_enabled() -> bool:
    return os.getenv("CTXSTORE_STDIO_MODE", "bridge").strip().lower() != "rest"


async def serve_list_tools() -> list[Tool]:
    if _bridge_enabled():
        try:
            remote = await bridge.list_tools()
            keep = ("name", "title", "description", "inputSchema", "annotations")
            return [Tool.model_validate({k: t[k] for k in keep if t.get(k) is not None}) for t in remote]
        except RemoteUnavailable as exc:
            logger.warning(f"hosted server unreachable ({exc}); serving the local REST tool set")
    return await list_tools()


async def serve_call_tool(name: str, arguments: dict[str, Any]) -> tuple[list[TextContent], bool]:
    global API_KEY
    if _bridge_enabled():
        try:
            items, is_error = await bridge.call_tool(name, arguments)
            if bridge.key_override:
                API_KEY = bridge.key_override  # keeps the REST fallback on the same account
            return [TextContent(type="text", text=i["text"] if i.get("type") == "text" else json.dumps(i))
                    for i in items], is_error
        except RemoteUnavailable as exc:
            if name not in _LOCAL_TOOLS:
                return [TextContent(type="text", text=f"{name} needs the hosted server, which is unreachable: {exc}")], True
    return await call_tool(name, arguments), False


def _build_app() -> Server:
    """Register the handlers on either mcp API: decorators (1.x) or constructor callbacks (2.x)."""
    if hasattr(Server, "list_tools"):  # mcp 1.x
        from mcp.types import CallToolResult

        server = Server("ctxstore", version=VERSION)

        async def _call(name: str, arguments: dict[str, Any]):
            content, is_error = await serve_call_tool(name, arguments)
            return CallToolResult(content=content, isError=is_error)

        server.list_tools()(serve_list_tools)
        server.call_tool(validate_input=False)(_call)
        return server

    from mcp.types import CallToolResult, ListToolsResult  # mcp 2.x

    async def _on_list(ctx, params):
        return ListToolsResult(tools=await serve_list_tools())

    async def _on_call(ctx, params):
        content, is_error = await serve_call_tool(params.name, params.arguments or {})
        return CallToolResult(content=content, isError=is_error)

    return Server("ctxstore", version=VERSION, on_list_tools=_on_list, on_call_tool=_on_call)


app = _build_app()


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

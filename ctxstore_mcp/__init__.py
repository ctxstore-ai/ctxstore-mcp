"""
ctxstore-mcp — Persistent AI memory via MCP.

A stdio MCP server that bridges to the hosted ctxstore server, so stdio-only
clients get the full toolset. Configuration is by environment variable; see
README.md (the key is TENANT_API_KEY).
"""

import asyncio
import sys

from .server import VERSION
from .server import main as _main

USAGE = f"""ctxstore-mcp {VERSION}: persistent memory for AI agents, as a stdio MCP server.

Run it from an MCP client config (command "uvx", args ["ctxstore-mcp"]).

Environment:
  TENANT_API_KEY       your emk_ API key; unset starts keyless (call setup_account)
  CTXSTORE_MCP_URL     hosted endpoint (default https://mcp.ctxstore.ai/mcp)
  CTXSTORE_STDIO_MODE  "bridge" (default) or "rest"

Docs: https://ctxstore.ai/for-agents  ·  https://github.com/ctxstore-ai/ctxstore-mcp
"""


def main():
    """CLI entry point."""
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE)
        return
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print(VERSION)
        return
    asyncio.run(_main())

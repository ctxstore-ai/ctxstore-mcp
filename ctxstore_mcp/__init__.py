"""
ctxstore-mcp — Persistent AI memory via MCP.

Thin MCP server that proxies to the ctxstore.ai API.
All embedding and storage happens server-side.
"""

import asyncio

from .server import main as _main


def main():
    """CLI entry point."""
    asyncio.run(_main())

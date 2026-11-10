"""
ctxstore-setup: show, and on request write, the ctxstore MCP entry for the
clients installed on this machine.

By default it changes nothing. It prints what it found and the exact entry
each client needs, so you can paste it or let it write with ``--write``.

Clients that speak HTTP (Claude Code, Cursor, VS Code, Windsurf) get the hosted
server ``https://mcp.ctxstore.ai/mcp`` with no key in the file: the agent
signs in with ``setup_account`` on first use. Claude Desktop runs the stdio
edition (``uvx ctxstore-mcp``); a saved key, if there is one, goes in its env.

Claude Code is written at PROJECT scope (``./.mcp.json`` in the current
directory) unless ``--global`` is given, so one project's setup never changes
another's. That is the same as:

    claude mcp add --transport http --scope project ctxstore https://mcp.ctxstore.ai/mcp
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple

import httpx

from .auth import load_credentials

MCP_URL = "https://mcp.ctxstore.ai/mcp"
HEALTH_URL = "https://ctxstore.ai/health"


class Client(NamedTuple):
    name: str
    slug: str
    config_path: Path
    servers_key_path: list[str]
    entry: dict
    # The client counts as installed when this path exists (the config file, or
    # the directory that would hold it).
    detect_path: Path


def _entries(api_key: str) -> dict[str, dict]:
    stdio: dict = {"command": "uvx", "args": ["ctxstore-mcp"]}
    if api_key:
        stdio["env"] = {"TENANT_API_KEY": api_key}
    return {
        "claude-code": {"type": "http", "url": MCP_URL},
        "cursor": {"url": MCP_URL},
        "vscode": {"type": "http", "url": MCP_URL},
        "windsurf": {"serverUrl": MCP_URL},
        "claude-desktop": stdio,
    }


def candidates(home: Path, cwd: Path, api_key: str, global_claude_code: bool) -> list[Client]:
    e = _entries(api_key)
    claude_code_path = home / ".claude.json" if global_claude_code else cwd / ".mcp.json"
    return [
        Client(
            "Claude Code" + (" (user scope)" if global_claude_code else " (project scope)"),
            "claude-code",
            claude_code_path,
            ["mcpServers"],
            e["claude-code"],
            home / ".claude" if not global_claude_code else home / ".claude.json",
        ),
        Client("Cursor", "cursor", home / ".cursor" / "mcp.json", ["mcpServers"], e["cursor"], home / ".cursor"),
        Client("VS Code (user)", "vscode", home / ".vscode" / "mcp.json", ["servers"], e["vscode"], home / ".vscode"),
        Client("Windsurf", "windsurf", home / ".windsurf" / "mcp.json", ["mcpServers"], e["windsurf"], home / ".windsurf"),
        Client(
            "Claude Desktop (macOS)",
            "claude-desktop",
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            ["mcpServers"],
            e["claude-desktop"],
            home / "Library" / "Application Support" / "Claude",
        ),
        Client(
            "Claude Desktop (Linux/Windows)",
            "claude-desktop",
            home / ".config" / "claude" / "claude_desktop_config.json",
            ["mcpServers"],
            e["claude-desktop"],
            home / ".config" / "claude",
        ),
    ]


def _load_config(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            print(f"  warning: {path} is not valid JSON; leaving it alone", file=sys.stderr)
            raise
    return {}


def _get_nested(d: dict, keys: list[str]) -> dict:
    for k in keys:
        d = d.setdefault(k, {})
    return d


def plan(clients: list[Client], only: set[str] | None) -> list[tuple[Client, str]]:
    """Return (client, state) for each installed client: 'add', 'update' or 'same'."""
    out: list[tuple[Client, str]] = []
    for c in clients:
        if only and c.slug not in only:
            continue
        if not (c.config_path.exists() or c.detect_path.exists()):
            continue
        try:
            config = _load_config(c.config_path)
        except json.JSONDecodeError:
            continue
        current = _get_nested(dict(config), c.servers_key_path).get("ctxstore")
        state = "same" if current == c.entry else ("update" if current else "add")
        out.append((c, state))
    return out


def write(c: Client) -> None:
    config = _load_config(c.config_path)
    _get_nested(config, c.servers_key_path)["ctxstore"] = c.entry
    c.config_path.parent.mkdir(parents=True, exist_ok=True)
    c.config_path.write_text(json.dumps(config, indent=2) + "\n")


def _health() -> bool:
    try:
        return httpx.get(HEALTH_URL, timeout=10.0).status_code == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ctxstore-setup", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--write", action="store_true", help="write the entries (default: only show them)")
    p.add_argument("--global", dest="global_claude_code", action="store_true", help="Claude Code: write ~/.claude.json instead of ./.mcp.json")
    p.add_argument("--client", action="append", choices=["claude-code", "cursor", "vscode", "windsurf", "claude-desktop"], help="limit to one client (repeatable)")
    p.add_argument("--dir", type=Path, default=Path.cwd(), help="project directory for Claude Code's .mcp.json (default: current directory)")
    args = p.parse_args(argv)

    creds = load_credentials() or {}
    api_key = str(creds.get("api_key") or "")
    clients = candidates(Path.home(), args.dir.resolve(), api_key, args.global_claude_code)
    found = plan(clients, set(args.client) if args.client else None)

    print("ctxstore-setup" + ("" if args.write else " (showing only; add --write to apply)") + "\n")
    if not found:
        print("No supported MCP client found here. Connect any HTTP client to " + MCP_URL)
        print("or run the stdio edition with: uvx ctxstore-mcp")
        return 0

    for c, state in found:
        shown = dict(c.entry)
        if "env" in shown:
            shown = {**shown, "env": {"TENANT_API_KEY": "<saved key>"}}
        print(f"[{state:6}] {c.name}: {c.config_path}")
        print("         " + json.dumps({"ctxstore": shown}))
        if c.slug == "claude-code" and not args.global_claude_code:
            print(f"         same as: claude mcp add --transport http --scope project ctxstore {MCP_URL}")
        if args.write and state != "same":
            write(c)
            print("         written")
    print()
    if not api_key:
        print("No saved key: that is fine. On first use the agent calls setup_account(mode='link_or_create', email=...)")
        print("and you read a six-digit code out of your email. Never paste a key into a chat.")
    print("ctxstore.ai is " + ("reachable." if _health() else "not reachable from here; check your connection."))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

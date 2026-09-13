"""
ctxstore-setup — one-command installer for ctxstore MCP across all supported clients.

Detects installed MCP clients, uses your saved key if there is one,
injects the ctxstore MCP entry, and runs a health check.
"""

import json
import sys
from pathlib import Path
from typing import NamedTuple

import httpx

from .auth import load_credentials, resolve_api_key

HEALTH_URL = "https://ctxstore.ai/api/health"

MCP_ENTRY = {
    "command": "ctxstore-mcp",
    "env": {},  # populated at runtime with the resolved key
}


class Client(NamedTuple):
    name: str
    config_path: Path
    # How the mcpServers dict is nested in the config file
    # e.g. ["mcpServers"] means config["mcpServers"]["ctxstore"] = ...
    servers_key_path: list[str]


def _candidate_paths() -> list[Client]:
    home = Path.home()
    return [
        Client(
            name="Claude Desktop (macOS)",
            config_path=home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            servers_key_path=["mcpServers"],
        ),
        Client(
            name="Claude Desktop (Linux/Windows)",
            config_path=home / ".config" / "claude" / "claude_desktop_config.json",
            servers_key_path=["mcpServers"],
        ),
        Client(
            name="Claude Code",
            config_path=home / ".claude.json",
            servers_key_path=["mcpServers"],
        ),
        Client(
            name="Cursor",
            config_path=home / ".cursor" / "mcp.json",
            servers_key_path=["mcpServers"],
        ),
        Client(
            name="VS Code (user)",
            config_path=home / ".vscode" / "mcp.json",
            servers_key_path=["servers"],
        ),
        Client(
            name="VS Code (workspace)",
            config_path=Path(".vscode") / "mcp.json",
            servers_key_path=["servers"],
        ),
        Client(
            name="Windsurf",
            config_path=home / ".windsurf" / "mcp.json",
            servers_key_path=["mcpServers"],
        ),
    ]


def _load_config(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: {path} contains invalid JSON — treating as empty.", file=sys.stderr)
    return {}


def _get_nested(d: dict, keys: list[str]) -> dict:
    for k in keys:
        d = d.setdefault(k, {})
    return d


def _inject_entry(config: dict, servers_key_path: list[str], api_key: str) -> bool:
    """
    Inject ctxstore entry into the servers dict.
    Returns True if a change was made.
    """
    servers = _get_nested(config, servers_key_path)
    entry = {
        "command": "ctxstore-mcp",
        "env": {"TENANT_API_KEY": api_key},
    }
    if servers.get("ctxstore") == entry:
        return False
    servers["ctxstore"] = entry
    return True


def _write_config(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _run_health_check() -> bool:
    try:
        resp = httpx.get(HEALTH_URL, timeout=10.0)
        return resp.status_code == 200
    except Exception:
        return False


def main() -> None:
    print("ctxstore-setup — configuring ctxstore MCP across your AI clients\n")

    # Step 1: Resolve API key (setup mode if none yet — the server prompts for the email-code flow)
    print("Step 1: Resolving API key...")
    api_key = resolve_api_key()
    if not api_key:
        print("  ERROR: Could not obtain an API key. Set TENANT_API_KEY and retry.", file=sys.stderr)
        sys.exit(1)

    creds = load_credentials()
    tenant_id = creds.get("tenant_id", "") if creds else ""
    masked = api_key[:8] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
    print(f"  API key: {masked}")
    if tenant_id:
        print(f"  Tenant:  {tenant_id}")
    print()

    # Step 2: Detect and configure clients
    print("Step 2: Detecting MCP clients...")
    candidates = _candidate_paths()
    configured: list[str] = []
    skipped: list[str] = []

    for client in candidates:
        path = client.config_path
        # Detect: config file exists OR parent dir exists (for clients that may not have config yet)
        parent_exists = path.parent.exists()
        config_exists = path.exists()

        if not config_exists and not parent_exists:
            continue  # client not installed

        config = _load_config(path)
        changed = _inject_entry(config, client.servers_key_path, api_key)

        if changed:
            _write_config(path, config)
            configured.append(f"  [+] {client.name}: {path}")
        else:
            skipped.append(f"  [=] {client.name}: already configured")

    if not configured and not skipped:
        print("  No MCP clients detected.")
        print("  Install Claude Desktop, Cursor, VS Code, or Windsurf and re-run.")
    else:
        for line in configured + skipped:
            print(line)
    print()

    # Step 3: Health check
    print("Step 3: Running health check against ctxstore.ai...")
    if _run_health_check():
        print("  API reachable — ctxstore.ai is up.\n")
    else:
        print("  Warning: could not reach ctxstore.ai — check your connection.\n", file=sys.stderr)

    # Summary
    print("Done!")
    if configured:
        print(f"  {len(configured)} client(s) configured. Restart your AI client to activate ctxstore.")
    if skipped:
        print(f"  {len(skipped)} client(s) were already up to date.")
    print()
    print("  Docs: https://ctxstore.ai/docs")
    print("  Dashboard: https://ctxstore.ai/dashboard")
